//! Whisper inference (English-only or multilingual) with Rust feature
//! extraction and tract kernels. ONNX supplies unchanged int8/f32 weights; no
//! foreign inference runtime.
mod embedding;
mod features;
mod graph;
mod model;
mod profile;
mod quantize;
mod scale;
mod tokens;
use embedding::Embedding;
use features::Features;
pub use profile::LANGUAGES;
use profile::Profile;
use std::{collections::BTreeMap, path::Path, sync::Arc};
use tokens::TokenTable;
use tract_linalg::multithread::Executor;
use tract_onnx::prelude::*;
use tract_onnx::tract_core::{
    internal::{ensure, format_err},
    runtime::RunOptions,
};

/// What a transcription found: the text and, for multilingual models, the
/// language it was decoded as.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Transcript {
    pub text: String,
    pub language: Option<&'static str>,
}
pub struct Recognizer {
    profile: Profile,
    encoder: Arc<TypedRunnableModel>,
    blocks: Vec<Arc<TypedRunnableModel>>,
    norm: Arc<TypedRunnableModel>,
    embedding: Embedding,
    positions: Tensor,
    tokens: TokenTable,
    pool: Arc<rayon::ThreadPool>,
    features: Features,
}
fn plan(model: TypedModel, pool: &Arc<rayon::ThreadPool>) -> TractResult<Arc<TypedRunnableModel>> {
    let model = graph::fold_weight_sums(model)?;
    let model = quantize::install(model)?;
    let model = scale::install(model)?;
    TypedRunnableModel::new_with_options(
        model,
        &RunOptions {
            executor: Some(Executor::MultiThread(pool.clone())),
            ..Default::default()
        },
    )
}
impl Recognizer {
    pub fn load(
        encoder: impl AsRef<Path>,
        decoder: impl AsRef<Path>,
        tokens: impl AsRef<Path>,
        threads: usize,
    ) -> TractResult<Self> {
        ensure!((1..=32).contains(&threads), "Expected 1–32 threads");
        let pool = Arc::new(
            rayon::ThreadPoolBuilder::new()
                .num_threads(threads)
                .thread_name(|n| format!("whisper-{n}"))
                .build()?,
        );
        let encoder = encoder.as_ref().canonicalize()?;
        let decoder = decoder.as_ref().canonicalize()?;
        let encoder_dir = encoder
            .parent()
            .and_then(Path::to_str)
            .ok_or_else(|| format_err!("Expected UTF-8 model directory"))?;
        let decoder_dir = decoder
            .parent()
            .and_then(Path::to_str)
            .ok_or_else(|| format_err!("Expected UTF-8 model directory"))?;
        let loader = tract_onnx::onnx();
        // Compile the encoder before adding resident decoder weights.
        let (mut p, _) = model::load(&encoder, 128 * 1024 * 1024, false)?;
        let profile = Profile::from_metadata(&p.metadata_props)?;
        ensure!(
            p.graph.as_ref().is_some_and(|g| g.input.len() == 1
                && g.input[0].name == "mel"
                && g.output
                    .iter()
                    .map(|v| v.name.as_str())
                    .eq(["n_layer_cross_k", "n_layer_cross_v"])),
            "Unexpected encoder interface"
        );
        graph::check_export(p.graph.as_ref().unwrap(), profile.encoder_wiring)?;
        graph::split_encoder_outputs(&mut p, profile.layers)?;
        graph::clear_shapes(&mut p)?;
        let parsed = loader.parse(&p, Some(encoder_dir))?;
        ensure!(
            parsed.unresolved_inputs.is_empty(),
            "Unresolved encoder inputs"
        );
        let mut model = parsed.model;
        drop(p);
        let f = model.symbols.sym("F");
        model.set_input_fact(0, f32::fact([1.to_dim(), 80.to_dim(), f.to_dim()]).into())?;
        let encoder = plan(model.into_optimized()?, &pool)?;

        let (p, embedding) = model::load(&decoder, 280 * 1024 * 1024, true)?;
        let embedding = embedding.ok_or_else(|| format_err!("Missing embedding"))?;
        let g = p
            .graph
            .as_ref()
            .ok_or_else(|| format_err!("Missing decoder graph"))?;
        graph::check_export(g, profile.decoder_wiring)?;
        ensure!(
            g.input.iter().map(|v| v.name.as_str()).eq([
                "tokens",
                "in_n_layer_self_k_cache",
                "in_n_layer_self_v_cache",
                "n_layer_cross_k",
                "n_layer_cross_v",
                "offset"
            ]),
            "Unexpected decoder interface"
        );
        let tensor = |name: &str| -> TractResult<Tensor> {
            let t = g
                .initializer
                .iter()
                .find(|t| t.name == name)
                .ok_or_else(|| format_err!("Missing {name}"))?;
            tract_onnx::tensor::load_tensor(&*loader.provider, t, Some(decoder_dir))
        };
        let positions = tensor("textDecoder.positional_embedding")?;
        ensure!(
            embedding.shape() == [profile.vocabulary().size, profile.state]
                && positions.shape() == [448, profile.state],
            "Unexpected embedding shape"
        );
        let blocks = (0..profile.layers)
            .map(|i| {
                plan(
                    graph::Builder::new(g, decoder_dir, profile).block(i)?,
                    &pool,
                )
            })
            .collect::<TractResult<Vec<_>>>()?;
        let norm = plan(
            graph::Builder::new(g, decoder_dir, profile).final_norm()?,
            &pool,
        )?;
        drop(p);

        let table = TokenTable::load(tokens.as_ref(), profile.vocabulary().text_tokens)?;
        Ok(Self {
            profile,
            encoder,
            blocks,
            norm,
            embedding,
            positions,
            tokens: table,
            pool,
            features: Features::default(),
        })
    }
    pub fn features(&self, samples: &[f32]) -> TractResult<(Tensor, usize)> {
        self.features.compute(samples)
    }
    pub fn model_name(&self) -> &'static str {
        self.profile.name
    }
    pub fn embedding_storage(&self) -> &'static str {
        self.embedding.storage()
    }
    pub fn embedding_bytes(&self) -> usize {
        self.embedding.bytes()
    }
    pub fn transcribe(&self, samples: &[f32]) -> TractResult<String> {
        Ok(self.recognize(samples, None, &[])?.text)
    }
    pub fn multilingual(&self) -> bool {
        self.profile.multilingual
    }
    /// Multilingual models only: decode as the most likely of `languages`
    /// (codes from [`LANGUAGES`]); one code fixes the language. Speech the
    /// model judges to be no speech at all returns empty text.
    pub fn transcribe_in(&self, samples: &[f32], languages: &[&str]) -> TractResult<Transcript> {
        ensure!(self.profile.multilingual, "This Whisper model is English-only");
        ensure!(
            !languages.is_empty() && languages.iter().all(|l| LANGUAGES.iter().any(|(c, _)| c == l)),
            "Unsupported transcription language"
        );
        self.recognize(samples, None, languages)
    }
    /// Explicit offline numerical diagnostic. Creates a new directory; never
    /// writes audio/intermediates during ordinary recognition.
    pub fn diagnose(&self, samples: &[f32], directory: &Path) -> TractResult<String> {
        std::fs::create_dir(directory)?;
        Ok(self.recognize(samples, Some(directory), &[])?.text)
    }
    fn recognize(
        &self,
        samples: &[f32],
        trace: Option<&Path>,
        languages: &[&str],
    ) -> TractResult<Transcript> {
        let vocabulary = self.profile.vocabulary();
        let state = self.profile.state;
        let layers = self.profile.layers;
        let mut profile = BTreeMap::new();
        let (features, frames) = self.features.compute(samples)?;
        if let Some(dir) = trace {
            write_tensor(&dir.join("mel.f32"), &features)?;
        }
        let encoder_start = std::time::Instant::now();
        let cross = run(
            &self.encoder,
            tvec!(features.into_tvalue()),
            trace.map(|_| &mut profile),
            "encoder",
        )?;
        let encoder_seconds = encoder_start.elapsed().as_secs_f64();
        let decoder_start = std::time::Instant::now();
        ensure!(
            cross.len() == layers * 2 && cross.iter().all(|t| t.rank() == 4),
            "Unexpected encoder outputs"
        );
        let time = cross[0].shape()[2];
        ensure!(
            cross.iter().all(|t| t.shape() == [1, 1, time, state]),
            "Unexpected cross-cache shape"
        );
        if let Some(dir) = trace {
            write_tensors(
                &dir.join("cross-k.f32"),
                cross[..layers].iter().map(|v| &**v),
            )?;
            write_tensors(
                &dir.join("cross-v.f32"),
                cross[layers..].iter().map(|v| &**v),
            )?;
        }
        let mut cross = cross
            .into_iter()
            .map(|t| Ok(t.into_tensor().into_shape(&[1, time, state])?.into_tvalue()));
        let keys = cross
            .by_ref()
            .take(layers)
            .collect::<TractResult<Vec<_>>>()?;
        let values = cross.collect::<TractResult<Vec<_>>>()?;
        let mut k = (0..layers)
            .map(|_| Ok(Tensor::zero::<f32>(&[1, 0, state])?.into_tvalue()))
            .collect::<TractResult<Vec<_>>>()?;
        let mut v = k.clone();
        let mut language = None;
        let mut input = if self.profile.multilingual {
            // One step from <|startoftranscript|> scores every language and
            // "no speech"; then decode from a fresh cache with the full prompt.
            let first = self.step(&[vocabulary.sot], 0, &mut k.clone(), &mut v.clone(), &keys, &values)?;
            let code = *languages
                .iter()
                .max_by(|a, b| {
                    let score = |c: &&str| first[LANGUAGES.iter().find(|(l, _)| l == c).unwrap().1];
                    score(a).total_cmp(&score(b))
                })
                .unwrap_or(&"en");
            if no_speech(&first, vocabulary.no_speech) > 0.6 {
                return Ok(Transcript {
                    text: String::new(),
                    language: Some(LANGUAGES.iter().find(|(l, _)| *l == code).unwrap().0),
                });
            }
            let token = LANGUAGES.iter().find(|(l, _)| *l == code).unwrap();
            language = Some(token.0);
            vec![vocabulary.sot, token.1, vocabulary.transcribe, vocabulary.no_timestamps]
        } else {
            vec![50257, 50362]
        };
        let mut offset = 0;
        let mut bytes = Vec::new();
        let mut predicted = Vec::new();
        let maximum = ((frames as f64 / 100.0 * 6.0) as usize).min(224);
        for step in 0..=maximum {
            let n = input.len();
            let mut x = Vec::with_capacity(n * state);
            let positions = self.positions.try_as_plain_ram()?.as_slice::<f32>()?;
            for (i, &token) in input.iter().enumerate() {
                self.embedding.append_positioned(
                    token,
                    &positions[(offset + i) * state..(offset + i + 1) * state],
                    &mut x,
                )?;
            }
            let mut x = Tensor::from_shape(&[1, n, state], &x)?.into_tvalue();
            let total = offset + n;
            let mask: Vec<f32> = (0..n)
                .flat_map(|i| {
                    (0..total).map(move |j| {
                        if n > 1 && j > i {
                            f32::NEG_INFINITY
                        } else {
                            0.0
                        }
                    })
                })
                .collect();
            let mask = Tensor::from_shape(&[1, 1, n, total], &mask)?.into_tvalue();
            for layer in 0..layers {
                let out = run(
                    &self.blocks[layer],
                    tvec!(
                        x,
                        k[layer].clone(),
                        v[layer].clone(),
                        keys[layer].clone(),
                        values[layer].clone(),
                        mask.clone()
                    ),
                    trace.map(|_| &mut profile),
                    "decoder",
                )?;
                let mut out = out.into_iter();
                x = out.next().unwrap();
                k[layer] = out.next().unwrap();
                v[layer] = out.next().unwrap();
            }
            let last = Tensor::from_shape(
                &[1, 1, state],
                &x.try_as_plain_ram()?.as_slice::<f32>()?[(n - 1) * state..n * state],
            )?
            .into_tvalue();
            let norm = run(
                &self.norm,
                tvec!(last),
                trace.map(|_| &mut profile),
                "final_norm",
            )?;
            let x = norm[0].try_as_plain_ram()?.as_slice::<f32>()?;
            let logits = self.embedding.project(x, &self.pool)?;
            ensure!(
                logits.iter().all(|x| x.is_finite()),
                "Non-finite decoder logits"
            );
            // Multilingual decoding never emits a special token other than
            // end-of-text; English decoding is unchanged.
            let choices = if self.profile.multilingual {
                &logits[..=vocabulary.eot]
            } else {
                &logits[..]
            };
            let token = choices
                .iter()
                .enumerate()
                .fold(0, |best, (i, v)| if *v > choices[best] { i } else { best });
            if let Some(dir) = trace {
                write_tensor(
                    &dir.join(format!("step-{step:03}.f32")),
                    &Tensor::from_shape(&[vocabulary.size], &logits)?,
                )?;
                predicted.push(token);
            }
            if token == vocabulary.eot || step == maximum {
                break;
            }
            if let Some(text) = self.tokens.get(token) {
                bytes.extend_from_slice(text);
            }
            offset += n;
            input.clear();
            input.push(token);
        }
        let text = token_text(&bytes);
        let text = if self.profile.multilingual {
            collapse_repeats(&text)
        } else {
            text
        };
        if let Some(dir) = trace {
            std::fs::write(
                dir.join("trace.json"),
                serde_json::to_vec_pretty(
                    &serde_json::json!({"text":text,"tokens":predicted,"encoder_seconds":encoder_seconds,"decoder_seconds":decoder_start.elapsed().as_secs_f64(),"feature_frames":frames,"mel_shape":[1,80,frames+1000],"cross_shape":[layers,1,time,state],"profile":profile}),
                )?,
            )?;
        }
        Ok(Transcript { text, language })
    }
    /// One decoder pass over `input` at `offset`, returning the last position's
    /// logits and extending the self-attention caches.
    fn step(
        &self,
        input: &[usize],
        offset: usize,
        k: &mut [TValue],
        v: &mut [TValue],
        keys: &[TValue],
        values: &[TValue],
    ) -> TractResult<Vec<f32>> {
        let state = self.profile.state;
        let n = input.len();
        let positions = self.positions.try_as_plain_ram()?.as_slice::<f32>()?;
        let mut x = Vec::with_capacity(n * state);
        for (i, &token) in input.iter().enumerate() {
            self.embedding.append_positioned(
                token,
                &positions[(offset + i) * state..(offset + i + 1) * state],
                &mut x,
            )?;
        }
        let mut x = Tensor::from_shape(&[1, n, state], &x)?.into_tvalue();
        let total = offset + n;
        let mask: Vec<f32> = (0..n)
            .flat_map(|i| (0..total).map(move |j| if n > 1 && j > i { f32::NEG_INFINITY } else { 0.0 }))
            .collect();
        let mask = Tensor::from_shape(&[1, 1, n, total], &mask)?.into_tvalue();
        for layer in 0..self.profile.layers {
            let out = self.blocks[layer].run(tvec!(
                x,
                k[layer].clone(),
                v[layer].clone(),
                keys[layer].clone(),
                values[layer].clone(),
                mask.clone()
            ))?;
            let mut out = out.into_iter();
            x = out.next().unwrap();
            k[layer] = out.next().unwrap();
            v[layer] = out.next().unwrap();
        }
        let last = Tensor::from_shape(
            &[1, 1, state],
            &x.try_as_plain_ram()?.as_slice::<f32>()?[(n - 1) * state..n * state],
        )?
        .into_tvalue();
        let norm = self.norm.run(tvec!(last))?;
        let logits = self.embedding.project(norm[0].try_as_plain_ram()?.as_slice::<f32>()?, &self.pool)?;
        ensure!(logits.iter().all(|x| x.is_finite()), "Non-finite decoder logits");
        Ok(logits)
    }
}
/// Probability of the no-speech token at the first step.
fn no_speech(logits: &[f32], token: usize) -> f32 {
    let max = logits.iter().copied().fold(f32::NEG_INFINITY, f32::max);
    let sum: f32 = logits.iter().map(|l| (l - max).exp()).sum();
    (logits[token] - max).exp() / sum
}
/// Greedy decoding can loop on one phrase ("شكرا شكرا شكرا …"). Keep a word
/// sequence's first occurrence when it repeats three or more times in a row.
fn collapse_repeats(text: &str) -> String {
    let mut words: Vec<&str> = text.split_whitespace().collect();
    for size in 1..=4 {
        let mut i = 0;
        while i + size * 3 <= words.len() {
            let run = (1..)
                .take_while(|r| {
                    i + (r + 1) * size <= words.len()
                        && words[i + r * size..i + (r + 1) * size] == words[i..i + size]
                })
                .count();
            if run >= 2 {
                words.drain(i + size..i + (run + 1) * size);
            }
            i += 1;
        }
    }
    words.join(" ")
}
/// Byte-level BPE pieces can stop inside a multi-byte character (at the token
/// limit, or on noise). Drop incomplete sequences instead of failing the command.
fn token_text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .replace('\u{FFFD}', "")
        .trim()
        .to_owned()
}
fn run(
    plan: &Arc<TypedRunnableModel>,
    inputs: TVec<TValue>,
    profile: Option<&mut BTreeMap<String, (usize, f64)>>,
    stage: &str,
) -> TractResult<TVec<TValue>> {
    let Some(profile) = profile else {
        return plan.run(inputs);
    };
    plan.spawn()?
        .run_plan_with_eval(inputs, |context, state, node, inputs| {
            let start = std::time::Instant::now();
            let result = tract_onnx::tract_core::plan::eval(context, state, node, inputs);
            let entry = profile
                .entry(format!("{stage}/{} [{}]", node.name, node.op.name()))
                .or_default();
            entry.0 += 1;
            entry.1 += start.elapsed().as_secs_f64();
            result
        })
}
fn write_tensor(path: &Path, tensor: &Tensor) -> TractResult<()> {
    write_tensors(path, std::iter::once(tensor))
}
fn write_tensors<'a>(path: &Path, tensors: impl Iterator<Item = &'a Tensor>) -> TractResult<()> {
    use std::io::Write;
    let mut out = std::io::BufWriter::new(
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)?,
    );
    for tensor in tensors {
        for value in tensor.try_as_plain_ram()?.as_slice::<f32>()? {
            out.write_all(&value.to_le_bytes())?;
        }
    }
    out.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn token_text_drops_incomplete_utf8_instead_of_failing() {
        assert_eq!(token_text(b" open firefox "), "open firefox");
        assert_eq!(token_text(" café ♪".as_bytes()), "café ♪");
        // "♪" is E2 99 AA; a token limit can stop after its first two bytes.
        assert_eq!(token_text(b" open steam \xE2\x99"), "open steam");
        assert_eq!(token_text(b"\xFF go \xC3"), "go");
    }
    #[test]
    fn repeated_phrases_collapse_but_ordinary_repeats_stay() {
        assert_eq!(collapse_repeats("شكرا شكرا شكرا شكرا"), "شكرا");
        assert_eq!(collapse_repeats("go go"), "go go");
        assert_eq!(collapse_repeats("a b a b a b c"), "a b c");
        assert_eq!(collapse_repeats("open the door"), "open the door");
    }
}
