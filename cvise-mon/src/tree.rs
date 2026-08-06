//! What the reduction has removed, measured rather than inferred.
//!
//! Bytes are not a measure of a reduction. They move when a pass strips
//! whitespace and they move when it deletes a translation unit, and the two
//! read the same. What a person judges a reduction by is how much CODE is
//! left: how many files still have anything in them, how many lines of it
//! there are, and how many functions are still defined. Those are the three
//! numbers here, each as `before -> now`.
//!
//! `before` is not remembered from somewhere. The reduced tree is a git
//! worktree, so the original is a commit in it: the newest one that C-Vise did
//! not write. It is extracted once and the result cached against its hash.
//!
//! Functions are counted by `treesitter_delta list-definitions`, which is the
//! binary the reduction itself parses with. A second parser -- even the same
//! grammar from crates.io -- could drift from it and report a different number
//! about the same file, and then the screen would be arguing with the run.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

pub const SOURCE_SUFFIXES: [&str; 9] = [
    "cpp", "cc", "cxx", "c", "hpp", "hh", "hxx", "h", "inc",
];

/// How much code there is. Not how many bytes it occupies.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Shape {
    /// Files with at least one line that is not whitespace. A pass that empties
    /// a file but leaves it on disk has removed it as far as anyone reading the
    /// result is concerned, and this counts it that way.
    pub files: usize,
    /// Lines that are not blank -- the same rule C-Vise counts by, so the two
    /// agree.
    pub lines: usize,
    /// Function definitions, at any nesting depth.
    pub functions: usize,
}

impl Shape {
    pub fn removed_from(&self, before: Shape) -> (usize, usize, usize) {
        (
            before.files.saturating_sub(self.files),
            before.lines.saturating_sub(self.lines),
            before.functions.saturating_sub(self.functions),
        )
    }
}

pub fn is_source(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| SOURCE_SUFFIXES.contains(&e))
        .unwrap_or(false)
}

/// Every source file under `root`, with `.git` left out of it.
pub fn sources(root: &Path) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let Ok(kind) = entry.file_type() else { continue };
            if kind.is_dir() {
                if path.file_name().map(|n| n == ".git").unwrap_or(false) {
                    continue;
                }
                stack.push(path);
            } else if kind.is_file() && is_source(&path) {
                found.push(path);
            }
        }
    }
    found.sort();
    found
}

/// Non-blank lines, and whether the file has any.
fn lines_of_code(path: &Path) -> usize {
    let Ok(text) = std::fs::read(path) else {
        return 0;
    };
    String::from_utf8_lossy(&text)
        .lines()
        .filter(|line| !line.trim().is_empty())
        .count()
}

/// The tool the reduction parses with, wherever this checkout keeps it.
///
/// Not searched for on PATH: a `treesitter_delta` from somewhere else is a
/// different grammar, and the number it returns would quietly disagree with the
/// run's own.
pub fn treesitter_delta(repo: &Path) -> Option<PathBuf> {
    let candidates = [
        std::env::var("CVISE_TREESITTER_DELTA").ok().map(PathBuf::from),
        Some(repo.join("build/treesitter_delta/treesitter_delta")),
        Some(PathBuf::from("/usr/local/libexec/cvise/treesitter_delta")),
    ];
    candidates.into_iter().flatten().find(|p| p.is_file())
}

/// Function definitions per input file, in one invocation.
///
/// Multi-file mode takes the paths as a NUL-separated list on stdin and emits
/// one JSON object per definition. The first line is the vocabulary, and a
/// definition's `"p"` is an index counted from the end of it, so the file a
/// definition belongs to is known and the count can be cached per file.
pub fn count_functions(tool: &Path, files: &[PathBuf]) -> std::io::Result<Vec<usize>> {
    use std::io::Write;
    use std::process::{Command, Stdio};

    let mut per_file = vec![0usize; files.len()];
    if files.is_empty() {
        return Ok(per_file);
    }
    let mut child = Command::new(tool)
        .arg("list-definitions")
        .arg("--")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    {
        let mut stdin = child.stdin.take().expect("stdin was piped");
        let mut payload = Vec::new();
        for file in files {
            payload.extend_from_slice(file.as_os_str().as_encoded_bytes());
            payload.push(0);
        }
        stdin.write_all(&payload)?;
    }
    let out = child.wait_with_output()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let mut lines = text.lines();
    // The vocabulary is a JSON array and is empty for this transformation, but
    // its length is what "p" is offset by, so it is read rather than assumed.
    let vocabulary = lines
        .next()
        .map(|line| line.matches('"').count() / 2)
        .unwrap_or(0);
    for line in lines {
        let Some(at) = line.find("\"p\":") else { continue };
        let digits: String = line[at + 4..]
            .chars()
            .take_while(|c| c.is_ascii_digit())
            .collect();
        let Ok(id) = digits.parse::<usize>() else {
            continue;
        };
        if let Some(slot) = per_file.get_mut(id.saturating_sub(vocabulary)) {
            *slot += 1;
        }
    }
    Ok(per_file)
}

/// What a file was last measured as, and the stamp that says it still holds.
#[derive(Clone, Copy)]
struct Entry {
    mtime: i128,
    size: u64,
    lines: usize,
    functions: usize,
}

/// Per-file measurements, so a refresh costs the files that changed.
///
/// A publication rewrites some twenty files out of a thousand. Measuring all of
/// them every ten seconds spent 14 seconds of CPU per refresh -- taken from the
/// reduction, which is the only thing on this machine that should be using it.
pub struct Cache {
    path: PathBuf,
    entries: HashMap<PathBuf, Entry>,
}

impl Cache {
    pub fn at(path: PathBuf) -> Self {
        let mut entries = HashMap::new();
        if let Ok(text) = std::fs::read_to_string(&path) {
            for line in text.lines() {
                let fields: Vec<&str> = line.split('\t').collect();
                if fields.len() != 5 {
                    continue;
                }
                let (Ok(mtime), Ok(size), Ok(lines), Ok(functions)) = (
                    fields[1].parse(),
                    fields[2].parse(),
                    fields[3].parse(),
                    fields[4].parse(),
                ) else {
                    continue;
                };
                entries.insert(
                    PathBuf::from(fields[0]),
                    Entry {
                        mtime,
                        size,
                        lines,
                        functions,
                    },
                );
            }
        }
        Cache { path, entries }
    }

    fn save(&self) {
        let mut text = String::with_capacity(self.entries.len() * 64);
        for (path, entry) in &self.entries {
            text.push_str(&format!(
                "{}\t{}\t{}\t{}\t{}\n",
                path.display(),
                entry.mtime,
                entry.size,
                entry.lines,
                entry.functions
            ));
        }
        let _ = std::fs::write(&self.path, text);
    }
}

/// The shape of a tree as it stands, re-reading only what changed.
pub fn measure(root: &Path, tool: Option<&Path>, cache: &mut Cache) -> Shape {
    use std::os::unix::fs::MetadataExt;

    let files = sources(root);
    let mut stale: Vec<PathBuf> = Vec::new();
    let mut stamps: HashMap<PathBuf, (i128, u64)> = HashMap::new();

    for file in &files {
        let Ok(meta) = std::fs::metadata(file) else {
            continue;
        };
        let stamp = (meta.mtime() as i128 * 1_000_000_000 + meta.mtime_nsec() as i128, meta.size());
        stamps.insert(file.clone(), stamp);
        match cache.entries.get(file) {
            Some(entry) if entry.mtime == stamp.0 && entry.size == stamp.1 => {}
            _ => stale.push(file.clone()),
        }
    }

    // Lines first, because a file with no code in it has no definitions either
    // and does not need parsing.
    let mut fresh_lines: HashMap<PathBuf, usize> = HashMap::new();
    let mut to_parse: Vec<PathBuf> = Vec::new();
    for file in &stale {
        let lines = lines_of_code(file);
        fresh_lines.insert(file.clone(), lines);
        if lines > 0 {
            to_parse.push(file.clone());
        }
    }

    let parsed = match tool {
        Some(tool) => count_functions(tool, &to_parse).unwrap_or_else(|_| vec![0; to_parse.len()]),
        None => vec![0; to_parse.len()],
    };
    let mut functions: HashMap<&PathBuf, usize> = HashMap::new();
    for (file, count) in to_parse.iter().zip(parsed) {
        functions.insert(file, count);
    }

    for file in &stale {
        let Some(&stamp) = stamps.get(file) else {
            continue;
        };
        cache.entries.insert(
            file.clone(),
            Entry {
                mtime: stamp.0,
                size: stamp.1,
                lines: fresh_lines.get(file).copied().unwrap_or(0),
                functions: functions.get(file).copied().unwrap_or(0),
            },
        );
    }
    // A file the reduction deleted must leave the cache, or the totals keep
    // counting code that is not there any more.
    cache.entries.retain(|path, _| stamps.contains_key(path));
    cache.save();

    let mut shape = Shape::default();
    for entry in cache.entries.values() {
        if entry.lines > 0 {
            shape.files += 1;
            shape.lines += entry.lines;
            shape.functions += entry.functions;
        }
    }
    shape
}

/// The newest commit in the worktree that C-Vise did not write.
///
/// The reduction commits its own progress under a fixed author, so the original
/// is simply the first one below them. Asked of the log rather than remembered
/// in a file: a remembered answer goes stale the moment somebody rebases, and
/// goes stale silently.
pub fn original_commit(worktree: &Path) -> Option<String> {
    let out = std::process::Command::new("git")
        .args(["-C"])
        .arg(worktree)
        .args(["log", "--format=%H\t%an", "-n", "200"])
        .output()
        .ok()?;
    for line in String::from_utf8_lossy(&out.stdout).lines() {
        let (sha, author) = line.split_once('\t')?;
        if author != "C-Vise" {
            return Some(sha.to_string());
        }
    }
    None
}

/// The shape of the tree before the reduction touched it.
///
/// Extracted once into the run's own state directory and cached against the
/// commit hash, because it cannot change while that hash does not.
pub fn baseline(state: &Path, worktree: &Path, tool: Option<&Path>) -> Option<(String, Shape)> {
    let sha = original_commit(worktree)?;
    let cache = state.join(format!("cvise-mon-baseline-{}.txt", &sha[..12]));
    if let Ok(text) = std::fs::read_to_string(&cache) {
        let numbers: Vec<usize> = text
            .split_whitespace()
            .filter_map(|n| n.parse().ok())
            .collect();
        if numbers.len() == 3 {
            return Some((
                sha,
                Shape {
                    files: numbers[0],
                    lines: numbers[1],
                    functions: numbers[2],
                },
            ));
        }
    }

    let extracted = state.join(format!("cvise-mon-original-{}", &sha[..12]));
    if !extracted.is_dir() {
        std::fs::create_dir_all(&extracted).ok()?;
        let archive = std::process::Command::new("git")
            .args(["-C"])
            .arg(worktree)
            .args(["archive", &sha])
            .output()
            .ok()?;
        if !archive.status.success() {
            return None;
        }
        let mut tar = std::process::Command::new("tar")
            .arg("-x")
            .arg("-C")
            .arg(&extracted)
            .stdin(std::process::Stdio::piped())
            .spawn()
            .ok()?;
        use std::io::Write;
        tar.stdin.as_mut()?.write_all(&archive.stdout).ok()?;
        tar.wait().ok()?;
    }

    let shape = measure(&extracted, tool, &mut Cache::at(state.join("cvise-mon-original.tsv")));
    let _ = std::fs::write(
        &cache,
        format!("{} {} {}\n", shape.files, shape.lines, shape.functions),
    );
    // The extracted copy is a hundred megabytes of tmpfs that the run itself
    // needs, and the three numbers are the whole of what it was for.
    let _ = std::fs::remove_dir_all(&extracted);
    let _ = std::fs::remove_file(state.join("cvise-mon-original.tsv"));
    Some((sha, shape))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("cvise-mon-test-{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn tool() -> Option<PathBuf> {
        treesitter_delta(Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap())
    }

    #[test]
    fn definitions_are_attributed_to_the_file_they_came_from() {
        // The count is cached per file, so a definition landing against the
        // wrong index would survive as a wrong number for as long as that file
        // is not touched again -- and it would look like a plausible number.
        let Some(tool) = tool() else {
            eprintln!("no treesitter_delta built; skipping");
            return;
        };
        let dir = scratch("attribution");
        std::fs::write(dir.join("one.cpp"), "int a(){return 1;}\n").unwrap();
        std::fs::write(
            dir.join("two.cpp"),
            "int b(){return 2;}\nint c(){return 3;}\n",
        )
        .unwrap();
        std::fs::write(dir.join("none.cpp"), "int declared_only(int);\n").unwrap();

        let files = vec![
            dir.join("one.cpp"),
            dir.join("two.cpp"),
            dir.join("none.cpp"),
        ];
        assert_eq!(count_functions(&tool, &files).unwrap(), vec![1, 2, 0]);
    }

    #[test]
    fn a_file_with_only_whitespace_is_not_a_file_that_is_left() {
        let dir = scratch("empty");
        std::fs::write(dir.join("gone.cpp"), "\n\n   \n\t\n").unwrap();
        std::fs::write(dir.join("here.cpp"), "int a(){return 1;}\n").unwrap();
        let mut cache = Cache::at(dir.join("cache.tsv"));
        let shape = measure(&dir, tool().as_deref(), &mut cache);
        assert_eq!(shape.files, 1);
        assert_eq!(shape.lines, 1);
    }

    #[test]
    fn a_changed_file_is_re_measured_and_a_deleted_one_stops_counting() {
        let dir = scratch("cache");
        let path = dir.join("a.cpp");
        std::fs::write(&path, "int a(){return 1;}\n").unwrap();
        let cache_file = dir.join("cache.tsv");

        let mut cache = Cache::at(cache_file.clone());
        assert_eq!(measure(&dir, tool().as_deref(), &mut cache).lines, 1);

        // Rewritten with more in it: the stamp differs, so it is read again.
        std::fs::write(&path, "int a(){return 1;}\nint b(){return 2;}\n").unwrap();
        let mut cache = Cache::at(cache_file.clone());
        let shape = measure(&dir, tool().as_deref(), &mut cache);
        assert_eq!(shape.lines, 2);
        if tool().is_some() {
            assert_eq!(shape.functions, 2);
        }

        // Deleted: it must leave the cache, or the totals keep counting code
        // that is not there.
        std::fs::remove_file(&path).unwrap();
        let mut cache = Cache::at(cache_file);
        let shape = measure(&dir, tool().as_deref(), &mut cache);
        assert_eq!(shape, Shape::default());
    }
}
