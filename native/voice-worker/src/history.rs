//! Explicit, unpaced decoder attribution probe. It never starts capture,
//! Whisper or action dispatch, and never changes the production reset policy.
use crate::{emit, engine::Engine, inference::trim, metrics, read_line, read_pcm, Payload, Result};
use serde_json::{json, Value};
use std::{io::BufRead, time::Instant};

fn memory() -> Value {
    let mut value = metrics();
    let pss = std::fs::read_to_string("/proc/self/smaps_rollup")
        .ok()
        .and_then(|text| {
            text.lines()
                .find(|line| line.starts_with("Pss:"))
                .and_then(|line| line.split_whitespace().nth(1))
                .and_then(|kb| kb.parse::<f64>().ok())
        });
    value["pss_mib"] = pss.map_or(Value::Null, |kb| json!(kb / 1024.0));
    value
}

fn keep(result: Value, texts: &mut Vec<String>) {
    if let Some(text) = result["text"].as_str().filter(|s| !s.is_empty()) {
        texts.push(text.to_owned());
    }
}

pub fn run(mut payload: Payload, reader: &mut impl BufRead) -> Result<()> {
    // Keep all three production decoders resident, but exercise exactly one.
    // This isolates its history growth from Whisper and the other decoders.
    payload.native.whisper = None;
    payload.native.denoiser = None;
    let engine = Engine::new(&payload.native, &payload.grammar, &payload.config)?;
    trim();
    emit(json!({"event":"ready","memory":memory()}))?;
    while let Some(line) = read_line(reader, 16384)? {
        let request: Value = serde_json::from_slice(&line)?;
        let name = request["decoder"].as_str().ok_or("Choose a decoder")?;
        let decoder = engine.history_decoder(name)?;
        let policy = request["policy"].as_str().unwrap_or("reset");
        if !["reset", "finalize"].contains(&policy) {
            return Err("Policy must be reset or finalize".into());
        }
        let cycles = request["cycles"].as_u64().unwrap_or(32);
        let seconds = request["seconds_per_cycle"].as_f64().unwrap_or(42.0);
        if !(3..=100).contains(&cycles) || !seconds.is_finite() || !(1.0..=60.0).contains(&seconds)
        {
            return Err("Use 3–100 cycles of 1–60 seconds".into());
        }
        let mut pcm = read_pcm(request["path"].as_str().ok_or("Fixture path required")?)?;
        if request["reverse"] == true {
            let mut samples = pcm.as_chunks::<2>().0.to_vec();
            samples.reverse();
            pcm = samples.into_iter().flatten().collect();
        }
        pcm.resize(pcm.len().div_ceil(960) * 960, 0);
        let frames = pcm.as_chunks::<960>().0;
        let cycle_frames = (seconds / 0.03).round() as usize;
        let started = Instant::now();
        let cpu = crate::cpu_seconds();
        let finalize = policy == "finalize";
        let mut running_frames = 0;
        for cycle in 0..cycles {
            let mut endpoints = 0;
            let mut texts = Vec::new();
            for index in 0..cycle_frames {
                // Same ten-second bound as Engine::decode; the probe uses
                // fixed-size utterances rather than an energy gate.
                if running_frames >= 333 {
                    if finalize {
                        keep(decoder.result(true)?, &mut texts);
                    } else {
                        decoder.reset();
                    }
                    running_frames = 0;
                }
                if decoder.accept(&frames[index % frames.len()][..])? {
                    keep(decoder.result(finalize)?, &mut texts);
                    endpoints += 1;
                    running_frames = 0;
                }
                running_frames += 1;
            }
            if finalize {
                keep(decoder.result(true)?, &mut texts);
            } else {
                decoder.reset();
            }
            running_frames = 0;
            let before_trim = memory();
            trim();
            emit(
                json!({"event":"cycle","decoder":name,"policy":policy,"cycle":cycle+1,
                "decoded_seconds":(cycle+1) as f64 * cycle_frames as f64 * 0.03,
                "elapsed_seconds":started.elapsed().as_secs_f64(),"cpu_seconds":crate::cpu_seconds()-cpu,
                "endpoints":endpoints,"texts":texts,"before_trim":before_trim,"memory":memory()}),
            )?;
        }
    }
    Ok(())
}
