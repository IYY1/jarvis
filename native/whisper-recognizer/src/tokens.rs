//! Dense token IDs index a packed byte arena, avoiding 50,256 tiny allocations.
use base64::{engine::general_purpose::STANDARD, Engine};
use std::{
    fs::File,
    io::{BufRead, BufReader, Read},
    path::Path,
};
use tract_onnx::prelude::*;
use tract_onnx::tract_core::internal::{ensure, format_err};

const TOKEN_COUNT: usize = 50256;
const MAX_BYTES: u64 = 2 * 1024 * 1024;
const MISSING: [u32; 2] = [u32::MAX; 2];

pub struct TokenTable {
    ranges: Box<[[u32; 2]]>,
    bytes: Box<[u8]>,
}
impl TokenTable {
    pub fn load(path: &Path) -> TractResult<Self> {
        let file = File::open(path)?;
        ensure!(
            file.metadata()?.len() <= MAX_BYTES,
            "Token table exceeds 2 MiB"
        );
        // Bound reads too, including a file that grows after the metadata check.
        Self::read(BufReader::new(file.take(MAX_BYTES + 1)), TOKEN_COUNT)
    }
    fn read(mut reader: impl BufRead, count: usize) -> TractResult<Self> {
        let mut ranges = vec![MISSING; count];
        let mut bytes = Vec::new();
        let mut line = String::new();
        let mut total = 0;
        let mut seen = 0;
        loop {
            line.clear();
            let n = reader.read_line(&mut line)?;
            if n == 0 {
                break;
            }
            total += n;
            ensure!(total <= MAX_BYTES as usize, "Token table exceeds 2 MiB");
            let (encoded, id) = line
                .trim_end_matches(['\r', '\n'])
                .split_once(' ')
                .ok_or_else(|| format_err!("Invalid token line"))?;
            let id: usize = id.parse()?;
            ensure!(id < count && ranges[id] == MISSING, "Invalid token ID");
            let start = bytes.len() as u32;
            STANDARD.decode_vec(encoded, &mut bytes)?;
            ranges[id] = [start, bytes.len() as u32];
            seen += 1;
        }
        ensure!(seen == count, "Unexpected English token table");
        Ok(Self {
            ranges: ranges.into_boxed_slice(),
            bytes: bytes.into_boxed_slice(),
        })
    }
    pub fn get(&self, id: usize) -> Option<&[u8]> {
        let &[start, end] = self.ranges.get(id)?;
        self.bytes.get(start as usize..end as usize)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dense_lookup_preserves_out_of_order_binary_and_empty_pieces() -> TractResult<()> {
        let table = TokenTable::read(&b"/wDD 2\r\nIA== 0\n 1\n"[..], 3)?;
        assert_eq!(table.get(0), Some(&b" "[..]));
        assert_eq!(table.get(1), Some(&b""[..]));
        assert_eq!(table.get(2), Some(&[255, 0, 195][..]));
        assert!(table.get(3).is_none());
        // Both backing allocations have no spare retained capacity.
        assert_eq!(
            table.ranges.len() * std::mem::size_of::<[u32; 2]>() + table.bytes.len(),
            28
        );
        Ok(())
    }
    #[test]
    fn rejects_duplicate_missing_invalid_and_special_ids() {
        for input in [
            "YQ== 0\nYg== 0\n",
            "YQ== 0\n",
            "YQ== 0\nYg== 2\n",
            "YQ== 0\n! 1\n",
            "YQ== 0\nYg== -1\n",
        ] {
            assert!(TokenTable::read(input.as_bytes(), 2).is_err(), "{input:?}");
        }
    }
}
