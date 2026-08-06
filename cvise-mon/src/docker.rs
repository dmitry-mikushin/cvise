//! Everything that has to be asked of docker, asked directly.
//!
//! Not through a shell. MEASURED on this machine, same command, same instant:
//! `docker logs <cid>` through a shell returned 16 lines and a "Log Summary"
//! digest with zero progress lines in it; the same call from a process returned
//! 657 lines and 23 progress lines. The environment replaces long output with a
//! plausible summary, and does not say so. Every reading here is taken by
//! spawning the program and reading its bytes.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

pub const IMAGE: &str = "ns-rtc-cvise";

pub fn output(program: &str, args: &[&str]) -> String {
    match Command::new(program).args(args).output() {
        Ok(out) => {
            let mut text = String::from_utf8_lossy(&out.stdout).into_owned();
            text.push_str(&String::from_utf8_lossy(&out.stderr));
            text
        }
        Err(_) => String::new(),
    }
}

#[derive(Clone, Debug)]
pub struct Reduction {
    pub id: String,
    pub status: String,
    pub state: PathBuf,
    pub worktree: PathBuf,
}

/// The running reduction, found by what it runs and what it has mounted.
///
/// Never by container name -- those are assigned at random -- and never by
/// image alone, because the verification builds and every one-off probe use the
/// same image.
pub fn reduction() -> Option<Reduction> {
    let listing = output(
        "docker",
        &["ps", "--format", "{{.ID}}\t{{.Image}}\t{{.Command}}\t{{.Status}}"],
    );
    for row in listing.lines() {
        let parts: Vec<&str> = row.split('\t').collect();
        if parts.len() != 4 || parts[1] != IMAGE {
            continue;
        }
        if !parts[2].trim_matches('"').starts_with("cvise") {
            continue;
        }
        let mounts = output(
            "docker",
            &["inspect", parts[0], "--format", "{{range .Mounts}}{{.Source}}\n{{end}}"],
        );
        let sources: Vec<PathBuf> = mounts.lines().map(PathBuf::from).collect();
        let state = sources.iter().find(|p| {
            p.parent() == Some(Path::new("/dev/shm"))
                && p.file_name()
                    .and_then(|n| n.to_str())
                    .map(|n| n.starts_with("cvise-"))
                    .unwrap_or(false)
        })?;
        // The worktree is mounted from inside the state directory; it is the
        // tree that is actually being reduced.
        let worktree = sources
            .iter()
            .find(|p| p.parent() == Some(state.as_path()))
            .cloned()
            .unwrap_or_else(|| state.join("ns-projection"));
        return Some(Reduction {
            id: parts[0].to_string(),
            status: parts[3].to_string(),
            state: state.clone(),
            worktree,
        });
    }
    None
}

/// A reduction container that is no longer running, for the message that says
/// why there is nothing to watch.
pub fn last_corpse() -> Option<(String, String)> {
    let listing = output(
        "docker",
        &["ps", "-a", "--format", "{{.ID}}\t{{.Image}}\t{{.Command}}"],
    );
    for row in listing.lines() {
        let parts: Vec<&str> = row.split('\t').collect();
        if parts.len() == 3 && parts[1] == IMAGE && parts[2].trim_matches('"').starts_with("cvise")
        {
            let state = output(
                "docker",
                &[
                    "inspect",
                    parts[0],
                    "--format",
                    "exit={{.State.ExitCode}} oom={{.State.OOMKilled}} finished={{.State.FinishedAt}}",
                ],
            );
            return Some((parts[0].to_string(), state.trim().to_string()));
        }
    }
    None
}

#[derive(Clone, Debug, Default)]
pub struct Load {
    pub cpu_percent: f64,
    pub mem_used: String,
    pub mem_limit: String,
}

pub fn load(id: &str) -> Load {
    let row = output(
        "docker",
        &["stats", "--no-stream", "--format", "{{.CPUPerc}}\t{{.MemUsage}}", id],
    );
    let parts: Vec<&str> = row.trim().split('\t').collect();
    if parts.len() != 2 {
        return Load::default();
    }
    let (used, limit) = parts[1].split_once(" / ").unwrap_or((parts[1], ""));
    Load {
        cpu_percent: parts[0].trim_end_matches('%').parse().unwrap_or(0.0),
        mem_used: used.trim().to_string(),
        mem_limit: limit.trim().to_string(),
    }
}

/// Processes by name, with zombies left out.
///
/// A zombie keeps the name of what it was, so a count that does not look at the
/// state answers "entries in the process table" while being read as "work being
/// done". In one run those were 44 and 4.
pub fn live_processes(id: &str) -> BTreeMap<String, usize> {
    let listing = output("docker", &["exec", id, "ps", "-eo", "stat=,comm="]);
    let mut counts = BTreeMap::new();
    for line in listing.lines() {
        let mut fields = line.split_whitespace();
        let (Some(state), Some(name)) = (fields.next(), fields.next()) else {
            continue;
        };
        if state.starts_with('Z') {
            continue;
        }
        *counts.entry(name.to_string()).or_insert(0) += 1;
    }
    counts
}

pub struct Journal {
    pub lines: Vec<String>,
}

impl Journal {
    pub fn of(id: &str) -> Self {
        Journal {
            lines: output("docker", &["logs", id]).lines().map(str::to_string).collect(),
        }
    }

    pub fn count(&self, needle: &str) -> usize {
        self.lines.iter().filter(|l| l.contains(needle)).count()
    }

    pub fn jobs(&self) -> String {
        for line in &self.lines {
            if let Some(rest) = line.split_once("up to ") {
                if let Some(count) = rest.1.split_whitespace().next() {
                    if count.chars().all(|c| c.is_ascii_digit()) {
                        return count.to_string();
                    }
                }
            }
        }
        "?".into()
    }

    /// The passes named by the newest progress line.
    pub fn via(&self) -> Option<String> {
        for line in self.lines.iter().rev() {
            if let Some(start) = line.find(", via ") {
                let tail = &line[start + 6..];
                return Some(tail.trim_end_matches(')').to_string());
            }
        }
        None
    }

    pub fn last_traceback(&self) -> Option<String> {
        let at = self
            .lines
            .iter()
            .rposition(|l| l.contains("Traceback (most recent call last)"))?;
        Some(self.lines[at..].join("\n"))
    }
}
