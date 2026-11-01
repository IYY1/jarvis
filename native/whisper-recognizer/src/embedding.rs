//! Lossless storage compaction, with f32 lookup and projection arithmetic.
//! The supported exports' f32 embedding values are exactly binary16 values.
//! Check every value before compacting; keep the original tensor otherwise.
use rayon::prelude::*;
use std::io::Read;
use tract_onnx::prelude::*;
use tract_onnx::tract_core::internal::{ensure, format_err};

enum Storage {
    Compact(Vec<f16>),
    Original(Tensor),
}
pub struct Embedding {
    storage: Storage,
    columns: usize,
}
impl Embedding {
    /// Convert the serialized table in bounded chunks, without a second full
    /// f32 copy. A nonexact value switches to the original f32 representation;
    /// every already-converted value round-trips exactly, including signed zero.
    pub fn read_raw(reader: &mut impl Read, shape: &[i64], bytes: u64) -> TractResult<Self> {
        ensure!(
            shape.len() == 2 && shape[0] > 0 && shape[1] > 0 && shape[1] % 8 == 0,
            "Unexpected embedding shape"
        );
        let rows = usize::try_from(shape[0])?;
        let columns = usize::try_from(shape[1])?;
        let count = rows
            .checked_mul(columns)
            .ok_or_else(|| format_err!("Embedding too large"))?;
        ensure!(
            count <= 51864 * 768 && u64::try_from(count)?.checked_mul(4) == Some(bytes),
            "Unexpected embedding byte length"
        );
        let mut compact = Vec::with_capacity(count);
        let mut buffer = [0_u8; 16384];
        let mut consumed = 0;
        while consumed < count {
            let n = (count - consumed).min(buffer.len() / 4);
            reader.read_exact(&mut buffer[..n * 4])?;
            for (i, raw) in buffer[..n * 4].as_chunks::<4>().0.iter().enumerate() {
                let value = f32::from_le_bytes(*raw);
                let half = f16::from_f32(value);
                if value.is_finite() && half.to_f32().to_bits() == value.to_bits() {
                    compact.push(half);
                } else {
                    let mut tensor = Tensor::zero::<f32>(&[rows, columns])?;
                    let mut plain = tensor.try_as_plain_ram_mut()?;
                    let output = plain.as_slice_mut::<f32>()?;
                    for (out, value) in output.iter_mut().zip(&compact) {
                        *out = value.to_f32();
                    }
                    drop(compact);
                    for (out, raw) in output[consumed + i..consumed + n]
                        .iter_mut()
                        .zip(buffer[i * 4..n * 4].as_chunks::<4>().0)
                    {
                        *out = f32::from_le_bytes(*raw);
                    }
                    consumed += n;
                    while consumed < count {
                        let n = (count - consumed).min(buffer.len() / 4);
                        reader.read_exact(&mut buffer[..n * 4])?;
                        for (out, raw) in output[consumed..consumed + n]
                            .iter_mut()
                            .zip(buffer[..n * 4].as_chunks::<4>().0)
                        {
                            *out = f32::from_le_bytes(*raw);
                        }
                        consumed += n;
                    }
                    return Ok(Self {
                        storage: Storage::Original(tensor),
                        columns,
                    });
                }
            }
            consumed += n;
        }
        Ok(Self {
            storage: Storage::Compact(compact),
            columns,
        })
    }
    pub fn new(tensor: Tensor) -> TractResult<Self> {
        ensure!(
            tensor.rank() == 2 && tensor.shape()[1] != 0 && tensor.shape()[1].is_multiple_of(8),
            "Unexpected embedding shape"
        );
        let columns = tensor.shape()[1];
        // Reserve exactly once: collecting a short-circuiting Option iterator
        // can otherwise grow/copy this large table repeatedly during import.
        let mut compact = Vec::with_capacity(tensor.len());
        let exact = tensor
            .try_as_plain_ram()?
            .as_slice::<f32>()?
            .iter()
            .all(|&value| {
                let half = f16::from_f32(value);
                if !value.is_finite() || half.to_f32().to_bits() != value.to_bits() {
                    return false;
                }
                compact.push(half);
                true
            });
        Ok(Self {
            storage: if exact {
                Storage::Compact(compact)
            } else {
                Storage::Original(tensor)
            },
            columns,
        })
    }
    pub fn storage(&self) -> &'static str {
        match self.storage {
            Storage::Compact(_) => "lossless-f16",
            Storage::Original(_) => "f32",
        }
    }
    pub fn shape(&self) -> [usize; 2] {
        let count = match &self.storage {
            Storage::Compact(values) => values.len(),
            Storage::Original(tensor) => tensor.len(),
        };
        [count / self.columns, self.columns]
    }
    pub fn bytes(&self) -> usize {
        match &self.storage {
            Storage::Compact(values) => values.len() * 2,
            Storage::Original(tensor) => tensor.len() * 4,
        }
    }
    pub fn append_positioned(
        &self,
        token: usize,
        position: &[f32],
        output: &mut Vec<f32>,
    ) -> TractResult<()> {
        ensure!(position.len() == self.columns, "Unexpected position shape");
        let start = token
            .checked_mul(self.columns)
            .ok_or_else(|| format_err!("Invalid token index"))?;
        let end = start
            .checked_add(self.columns)
            .ok_or_else(|| format_err!("Invalid token index"))?;
        match &self.storage {
            Storage::Compact(values) => {
                let row = values
                    .get(start..end)
                    .ok_or_else(|| format_err!("Invalid token index"))?;
                output.extend(
                    row.iter()
                        .zip(position)
                        .map(|(weight, position)| weight.to_f32() + *position),
                );
            }
            Storage::Original(tensor) => {
                let plain = tensor.try_as_plain_ram()?;
                let row = plain
                    .as_slice::<f32>()?
                    .get(start..end)
                    .ok_or_else(|| format_err!("Invalid token index"))?;
                output.extend(
                    row.iter()
                        .zip(position)
                        .map(|(weight, position)| *weight + *position),
                );
            }
        }
        Ok(())
    }
    pub fn project(&self, input: &[f32], pool: &rayon::ThreadPool) -> TractResult<Vec<f32>> {
        ensure!(input.len() == self.columns, "Unexpected projection shape");
        Ok(match &self.storage {
            Storage::Compact(values) => pool.install(|| {
                values
                    .par_chunks_exact(self.columns)
                    .map(|row| dot_half(row, input))
                    .collect()
            }),
            Storage::Original(tensor) => {
                let plain = tensor.try_as_plain_ram()?;
                let values = plain.as_slice::<f32>()?;
                pool.install(|| {
                    values
                        .par_chunks_exact(self.columns)
                        .map(|row| dot_f32(row, input))
                        .collect()
                })
            }
        })
    }
}
fn dot_f32(a: &[f32], b: &[f32]) -> f32 {
    let mut sums = [0.0_f32; 8];
    for (a, b) in a.as_chunks::<8>().0.iter().zip(b.as_chunks::<8>().0) {
        for j in 0..8 {
            sums[j] += a[j] * b[j];
        }
    }
    sums.into_iter().sum()
}
fn dot_portable(a: &[f16], b: &[f32]) -> f32 {
    let mut sums = [0.0_f32; 8];
    for (a, b) in a.as_chunks::<8>().0.iter().zip(b.as_chunks::<8>().0) {
        for j in 0..8 {
            sums[j] += a[j].to_f32() * b[j];
        }
    }
    sums.into_iter().sum()
}
fn dot_half(a: &[f16], b: &[f32]) -> f32 {
    #[cfg(target_arch = "x86_64")]
    if std::is_x86_feature_detected!("avx") && std::is_x86_feature_detected!("f16c") {
        // SAFETY: feature detection includes OS AVX support. Each iteration
        // loads one complete eight-value chunk from each validated row.
        return unsafe { dot_f16c(a, b) };
    }
    dot_portable(a, b)
}
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx,f16c")]
unsafe fn dot_f16c(a: &[f16], b: &[f32]) -> f32 {
    use std::arch::x86_64::*;
    let mut sums = _mm256_setzero_ps();
    for (a, b) in a.as_chunks::<8>().0.iter().zip(b.as_chunks::<8>().0) {
        // half::f16 is repr(transparent) over u16. Use unaligned loads; weights
        // expand exactly and multiplication/addition remain separate f32 ops.
        let weights = _mm256_cvtph_ps(_mm_loadu_si128(a.as_ptr().cast()));
        let input = _mm256_loadu_ps(b.as_ptr());
        sums = _mm256_add_ps(sums, _mm256_mul_ps(weights, input));
    }
    let mut values = [0.0_f32; 8];
    _mm256_storeu_ps(values.as_mut_ptr(), sums);
    values.into_iter().sum()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn compacts_exact_values_and_preserves_signed_zero_and_lookups() -> TractResult<()> {
        let values = [
            0.0,
            -0.0,
            1.0,
            -1.0,
            65504.0,
            -65504.0,
            f16::MIN_POSITIVE.to_f32(),
            f16::MIN_POSITIVE_SUBNORMAL.to_f32(),
        ];
        let embedding = Embedding::new(Tensor::from_shape(&[1, 8], &values)?)?;
        assert_eq!(embedding.storage(), "lossless-f16");
        assert_eq!(embedding.bytes(), 16);
        let positions = [-0.0; 8];
        let mut actual = vec![];
        embedding.append_positioned(0, &positions, &mut actual)?;
        for i in 0..8 {
            assert_eq!(actual[i].to_bits(), (values[i] + positions[i]).to_bits());
        }
        assert!(embedding
            .append_positioned(1, &positions, &mut actual)
            .is_err());
        Ok(())
    }
    #[test]
    fn nonexact_or_nonfinite_values_keep_original_f32() -> TractResult<()> {
        for value in [0.1, f32::from_bits(1), 65505.0, f32::INFINITY, f32::NAN] {
            let values = [value; 8];
            let embedding = Embedding::new(Tensor::from_shape(&[1, 8], &values)?)?;
            assert_eq!(embedding.storage(), "f32");
            assert_eq!(embedding.bytes(), 32);
        }
        Ok(())
    }
    #[test]
    fn streamed_fallback_preserves_bits_across_chunk_boundaries() -> TractResult<()> {
        for index in [0, 7, 4095, 4096, 8191] {
            for value in [
                0.1_f32,
                f32::from_bits(1),
                f32::INFINITY,
                f32::from_bits(0x7fc0_1234),
            ] {
                let mut values = vec![-0.0_f32; 8192];
                values[index] = value;
                let bytes: Vec<_> = values.iter().flat_map(|v| v.to_le_bytes()).collect();
                let embedding =
                    Embedding::read_raw(&mut bytes.as_slice(), &[1024, 8], bytes.len() as u64)?;
                assert_eq!(embedding.storage(), "f32");
                let Storage::Original(tensor) = embedding.storage else {
                    unreachable!()
                };
                assert_eq!(tensor.as_bytes(), native_bytes(&values));
            }
        }
        assert!(Embedding::read_raw(&mut [0_u8; 31].as_slice(), &[1, 8], 32).is_err());
        for (shape, length) in [
            (vec![1, 7], 28),
            (vec![-1, 8], 32),
            (vec![1, 8], 31),
            (vec![i64::MAX, 8], 32),
        ] {
            assert!(Embedding::read_raw(&mut [].as_slice(), &shape, length).is_err());
        }
        Ok(())
    }
    fn native_bytes(values: &[f32]) -> Vec<u8> {
        values.iter().flat_map(|v| v.to_ne_bytes()).collect()
    }
    #[test]
    fn portable_and_dispatched_projection_match_f32_bits() -> TractResult<()> {
        let pool = rayon::ThreadPoolBuilder::new().num_threads(3).build()?;
        // Check every finite binary16 encoding, including subnormals and signed
        // zero; shifted slices also exercise unaligned SIMD loads.
        let values: Vec<_> = (0..=u16::MAX)
            .map(f16::from_bits)
            .filter(|v| v.is_finite())
            .collect();
        let input: Vec<_> = (0..768).map(|i| (i % 37) as f32 / 17.0 - 1.0).collect();
        for offset in 0..8 {
            for half in values[offset..].chunks(768) {
                let mut padded = half.to_vec();
                padded.resize(768, f16::ZERO);
                let half = padded.as_slice();
                let full: Vec<_> = half.iter().map(|v| v.to_f32()).collect();
                let expected = dot_f32(&full, &input).to_bits();
                assert_eq!(dot_portable(half, &input).to_bits(), expected);
                assert_eq!(dot_half(half, &input).to_bits(), expected);
            }
        }
        let half: Vec<_> = input.iter().map(|v| f16::from_f32(*v).to_f32()).collect();
        let embedding = Embedding::new(Tensor::from_shape(&[1, 768], &half)?)?;
        assert_eq!(
            embedding.project(&input, &pool)?[0].to_bits(),
            dot_f32(&half, &input).to_bits()
        );
        // Exercise f32 fallback through the same public projection path.
        let original = Embedding::new(Tensor::from_shape(&[1, 768], &input)?)?;
        assert_eq!(original.storage(), "f32");
        assert_eq!(
            original.project(&input, &pool)?[0].to_bits(),
            dot_f32(&input, &input).to_bits()
        );
        Ok(())
    }
}
