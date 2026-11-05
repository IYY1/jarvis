//! Explicitly supported sherpa int8 exports (English-only and multilingual).
//! Never derive allocation sizes or attention dimensions from untrusted,
//! unconstrained metadata.
use std::collections::HashMap;
use tract_onnx::pb;
use tract_onnx::prelude::*;
use tract_onnx::tract_core::internal::{ensure, format_err};

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Profile {
    pub name: &'static str,
    pub state: usize,
    pub heads: usize,
    pub layers: usize,
    pub encoder_wiring: u64,
    pub decoder_wiring: u64,
    pub multilingual: bool,
}

/// Special token IDs, fixed by the English-only and multilingual vocabularies.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Vocabulary {
    pub size: usize,
    pub text_tokens: usize,
    pub sot: usize,
    pub eot: usize,
    pub no_speech: usize,
    pub no_timestamps: usize,
    pub transcribe: usize,
}
pub(crate) const ENGLISH: Vocabulary = Vocabulary {
    size: 51864,
    text_tokens: 50256,
    sot: 50257,
    eot: 50256,
    no_speech: 50361,
    no_timestamps: 50362,
    transcribe: 50358,
};
pub(crate) const MULTILINGUAL: Vocabulary = Vocabulary {
    size: 51865,
    text_tokens: 50257,
    sot: 50258,
    eot: 50257,
    no_speech: 50362,
    no_timestamps: 50363,
    transcribe: 50359,
};
/// Language tokens of the multilingual vocabulary (OpenAI's fixed order).
pub const LANGUAGES: [(&str, usize); 2] = [("en", 50259), ("ar", 50272)];
impl Profile {
    pub(crate) fn vocabulary(&self) -> Vocabulary {
        if self.multilingual {
            MULTILINGUAL
        } else {
            ENGLISH
        }
    }
}

const PROFILES: [Profile; 6] = [
    Profile {
        name: "tiny.en",
        state: 384,
        heads: 6,
        layers: 4,
        encoder_wiring: 0x7edb_33cc_b0a4_69c7,
        decoder_wiring: 0x64bf_822a_aa18_d652,
        multilingual: false,
    },
    Profile {
        name: "base.en",
        state: 512,
        heads: 8,
        layers: 6,
        encoder_wiring: 0xc58c_688c_93a6_a591,
        decoder_wiring: 0x032a_7dab_c126_532c,
        multilingual: false,
    },
    Profile {
        name: "small.en",
        state: 768,
        heads: 12,
        layers: 12,
        encoder_wiring: 0x8739_0418_f7ca_a917,
        decoder_wiring: 0xcd61_f134_021f_2a7b,
        multilingual: false,
    },
    // The multilingual exports share the English graphs' wiring; only the
    // vocabulary and prompt differ.
    Profile {
        name: "tiny",
        state: 384,
        heads: 6,
        layers: 4,
        encoder_wiring: 0x7edb_33cc_b0a4_69c7,
        decoder_wiring: 0x64bf_822a_aa18_d652,
        multilingual: true,
    },
    Profile {
        name: "base",
        state: 512,
        heads: 8,
        layers: 6,
        encoder_wiring: 0xc58c_688c_93a6_a591,
        decoder_wiring: 0x032a_7dab_c126_532c,
        multilingual: true,
    },
    Profile {
        name: "small",
        state: 768,
        heads: 12,
        layers: 12,
        encoder_wiring: 0x8739_0418_f7ca_a917,
        decoder_wiring: 0xcd61_f134_021f_2a7b,
        multilingual: true,
    },
];

impl Profile {
    pub fn from_metadata(properties: &[pb::StringStringEntryProto]) -> TractResult<Self> {
        let mut meta = HashMap::new();
        for property in properties {
            ensure!(
                meta.insert(property.key.as_str(), property.value.as_str())
                    .is_none(),
                "Duplicate Whisper metadata {}",
                property.key
            );
        }
        let selected = PROFILES
            .into_iter()
            .find(|p| {
                meta.get("model_type").copied() == Some(format!("whisper-{}", p.name).as_str())
            })
            .ok_or_else(|| {
                format_err!(
                    "Unsupported Whisper profile; expected tiny, base or small (English or multilingual) int8"
                )
            })?;
        let expected: &[(&str, &str)] = if selected.multilingual {
            &[
                ("n_vocab", "51865"),
                ("sot", "50258"),
                ("eot", "50257"),
                ("no_speech", "50362"),
                ("no_timestamps", "50363"),
                ("transcribe", "50359"),
                ("is_multilingual", "1"),
            ]
        } else {
            &[
                ("n_vocab", "51864"),
                ("sot_sequence", "50257"),
                ("eot", "50256"),
                ("no_timestamps", "50362"),
                ("is_multilingual", "0"),
            ]
        };
        for (key, value) in [
            ("version", "1"),
            ("n_mels", "80"),
            ("n_audio_ctx", "1500"),
            ("n_text_ctx", "448"),
        ]
        .iter()
        .chain(expected)
        {
            ensure!(
                meta.get(key).copied() == Some(*value),
                "Unsupported Whisper metadata {key}"
            );
        }
        for (key, value) in [
            ("n_audio_state", selected.state),
            ("n_text_state", selected.state),
            ("n_audio_head", selected.heads),
            ("n_text_head", selected.heads),
            ("n_audio_layer", selected.layers),
            ("n_text_layer", selected.layers),
        ] {
            ensure!(
                meta.get(key).copied() == Some(value.to_string().as_str()),
                "Unsupported Whisper metadata {key}"
            );
        }
        Ok(selected)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn metadata(profile: Profile) -> Vec<pb::StringStringEntryProto> {
        let m = profile.multilingual;
        [
            ("model_type", format!("whisper-{}", profile.name)),
            ("version", "1".into()),
            ("n_mels", "80".into()),
            ("n_audio_ctx", "1500".into()),
            ("n_text_ctx", "448".into()),
            ("n_vocab", if m { "51865" } else { "51864" }.into()),
            ("sot_sequence", if m { "50258,50259,50359" } else { "50257" }.into()),
            ("sot", if m { "50258" } else { "50257" }.into()),
            ("eot", if m { "50257" } else { "50256" }.into()),
            ("no_speech", if m { "50362" } else { "50361" }.into()),
            ("no_timestamps", if m { "50363" } else { "50362" }.into()),
            ("transcribe", if m { "50359" } else { "50358" }.into()),
            ("is_multilingual", if m { "1" } else { "0" }.into()),
            ("n_audio_state", profile.state.to_string()),
            ("n_text_state", profile.state.to_string()),
            ("n_audio_head", profile.heads.to_string()),
            ("n_text_head", profile.heads.to_string()),
            ("n_audio_layer", profile.layers.to_string()),
            ("n_text_layer", profile.layers.to_string()),
        ]
        .into_iter()
        .map(|(key, value)| pb::StringStringEntryProto {
            key: key.into(),
            value,
        })
        .collect()
    }
    #[test]
    fn only_explicit_profiles_with_matching_dimensions_are_accepted() {
        for profile in PROFILES {
            let meta = metadata(profile);
            assert_eq!(Profile::from_metadata(&meta).unwrap(), profile);
            for key in [
                "model_type",
                "n_audio_state",
                "n_text_head",
                "n_text_layer",
                "n_mels",
                "n_vocab",
                "no_timestamps",
                "is_multilingual",
            ] {
                let mut bad = meta.clone();
                bad.iter_mut().find(|p| p.key == key).unwrap().value = "999999".into();
                assert!(
                    Profile::from_metadata(&bad).is_err(),
                    "{}: {key}",
                    profile.name
                );
            }
            let mut duplicate = meta.clone();
            duplicate.push(meta[0].clone());
            assert!(Profile::from_metadata(&duplicate).is_err());
        }
    }
}
