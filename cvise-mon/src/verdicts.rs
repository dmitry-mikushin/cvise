//! The per-candidate journal, counted for the live run only.
//!
//! The file is appended to across runs that share a state directory, so a plain
//! count mixes a dead run's hundreds of thousands with a live run's hundreds.
//! The live root is whichever one the newest verdict names.

use std::collections::BTreeMap;
use std::path::Path;

#[derive(Clone, Debug, Default)]
pub struct Verdicts {
    pub total: usize,
    pub by_outcome: BTreeMap<String, usize>,
}

impl Verdicts {
    pub fn get(&self, outcome: &str) -> usize {
        self.by_outcome.get(outcome).copied().unwrap_or(0)
    }
}

fn job_root(line: &str) -> Option<&str> {
    let at = line.find("dir=")?;
    let rest = &line[at..];
    let start = rest.find("/cvise-")? + 1;
    let tail = &rest[start..];
    let end = tail.find("/job")?;
    Some(&tail[..end])
}

fn field<'a>(line: &'a str, name: &str) -> Option<&'a str> {
    let needle = format!("{name}=");
    let at = line.find(&needle)? + needle.len();
    let tail = &line[at..];
    Some(tail.split_whitespace().next().unwrap_or(tail))
}

pub fn read(state: &Path) -> Verdicts {
    let path = state.join("tmp/cvise-verdicts.log");
    let Ok(bytes) = std::fs::read(&path) else {
        return Verdicts::default();
    };
    let text = String::from_utf8_lossy(&bytes);
    let Some(root) = text.lines().rev().find_map(job_root) else {
        return Verdicts::default();
    };

    let mut verdicts = Verdicts::default();
    for line in text.lines() {
        if !line.contains(root) {
            continue;
        }
        verdicts.total += 1;
        if let Some(outcome) = field(line, "test") {
            *verdicts
                .by_outcome
                .entry(outcome.to_string())
                .or_insert(0) += 1;
        }
    }
    verdicts
}
