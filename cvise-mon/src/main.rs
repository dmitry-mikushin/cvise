//! cvise-mon -- what the running reduction has actually removed.
//!
//! Run it with no arguments while a reduction is going; it finds the container
//! itself. `--once` prints one plain-text screen instead of taking over the
//! terminal, which is what a script or a log wants.

mod docker;
mod pace;
mod tree;
mod ui;
mod verdicts;

use std::io;
use std::path::PathBuf;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyCode, KeyEventKind};

#[derive(Clone, Debug)]
pub struct Snapshot {
    pub worktree_name: String,
    pub status: String,
    pub jobs: String,
    pub original: String,
    pub before: tree::Shape,
    pub now: tree::Shape,
    pub via: Option<String>,
    /// The rhythm of the live run, read from the series C-Vise writes. This is
    /// what turns an open-ended wait into a deadline.
    pub pace: pace::Pace,
    pub now_epoch: f64,
    pub verdicts: verdicts::Verdicts,
    pub rate: Option<f64>,
    pub published: usize,
    pub baseline_failed: usize,
    pub load: docker::Load,
    pub cores: usize,
    pub shm_used: f64,
    pub shm_total: f64,
    /// None when the container could not be asked -- which is not the same
    /// as nothing running, and must not print like it.
    pub live: Option<Vec<(String, usize)>>,
    pub pass_bugs: usize,
    pub tracebacks: usize,
    pub traceback: Option<String>,
}

/// Free and total space of /dev/shm, in GiB.
///
/// Read from /proc/self/mountinfo and statvfs through `df`, because a reduction
/// that fills the tmpfs it lives in stops without saying why, and the number is
/// worth having on the screen before that happens.
fn shm() -> (f64, f64) {
    let text = docker::output("df", &["-B1", "--output=used,size", "/dev/shm"]);
    let numbers: Vec<f64> = text
        .split_whitespace()
        .filter_map(|n| n.parse::<f64>().ok())
        .collect();
    if numbers.len() >= 2 {
        (
            numbers[0] / 1024f64.powi(3),
            numbers[1] / 1024f64.powi(3),
        )
    } else {
        (0.0, 0.0)
    }
}

fn repo_root() -> PathBuf {
    // The checkout this binary was built in, which is where treesitter_delta is.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn gather(previous: Option<&Snapshot>, elapsed: Option<f64>) -> Option<Snapshot> {
    let run = docker::reduction()?;
    let journal = docker::Journal::of(&run.id);
    let tool = tree::treesitter_delta(&repo_root());
    let verdicts = verdicts::read(&run.state);
    let now_epoch = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let pace = pace::read(&run.state, now_epoch);

    let (original, before) = match tree::baseline(&run.state, &run.worktree, tool.as_deref()) {
        Some(pair) => pair,
        None => (String::from("unknown"), tree::Shape::default()),
    };
    let now = tree::measure(
        &run.worktree,
        tool.as_deref(),
        &mut tree::Cache::at(run.state.join("cvise-mon-now.tsv")),
    );

    let rate = match (previous, elapsed) {
        (Some(prev), Some(seconds)) if seconds > 0.0 => {
            Some((verdicts.total.saturating_sub(prev.verdicts.total)) as f64 / seconds)
        }
        _ => None,
    };

    let interesting = ["clang++-19", "clang_delta", "ninja", "cmake", "ccache", "python3"];
    let live = docker::live_processes(&run.id).map(|counts| {
        interesting
            .iter()
            .filter_map(|name| counts.get(*name).map(|n| (name.to_string(), *n)))
            .collect()
    });

    let (shm_used, shm_total) = shm();
    Some(Snapshot {
        worktree_name: run
            .worktree
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
        status: run.status.clone(),
        jobs: journal.jobs(),
        original,
        before,
        now,
        // From the series rather than from `docker logs`: same fact, read from
        // a file with a wall clock on it instead of scraped out of a stream.
        via: pace.via().or_else(|| journal.via()),
        published: pace.points.len().saturating_sub(1),
        pace,
        now_epoch,
        verdicts,
        rate,
        baseline_failed: journal.count("does not build as it stands"),
        load: docker::load(&run.id),
        cores: std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1),
        shm_used,
        shm_total,
        live,
        pass_bugs: journal.count("has encountered a non fatal bug"),
        tracebacks: journal.count("Traceback (most recent call last)"),
        traceback: journal.last_traceback(),
    })
}

fn plain(snap: &Snapshot) -> String {
    let (files, lines, functions) = snap.now.removed_from(snap.before);
    let percent = |gone: usize, before: usize| {
        if before == 0 {
            0.0
        } else {
            gone as f64 * 100.0 / before as f64
        }
    };
    let mut out = String::new();
    out.push_str(&format!(
        "cvise-mon  {}  {}  N={}\n\n",
        snap.worktree_name, snap.status, snap.jobs
    ));
    out.push_str(&format!(
        "reduced     files      {:>9} of {:>9}   -{} ({:.1}%)\n",
        ui::thousands(snap.now.files),
        ui::thousands(snap.before.files),
        ui::thousands(files),
        percent(files, snap.before.files),
    ));
    out.push_str(&format!(
        "            lines      {:>9} of {:>9}   -{} ({:.1}%)\n",
        ui::thousands(snap.now.lines),
        ui::thousands(snap.before.lines),
        ui::thousands(lines),
        percent(lines, snap.before.lines),
    ));
    out.push_str(&format!(
        "            functions  {:>9} of {:>9}   -{} ({:.1}%)\n",
        ui::thousands(snap.now.functions),
        ui::thousands(snap.before.functions),
        ui::thousands(functions),
        percent(functions, snap.before.functions),
    ));
    out.push_str(&format!(
        "            {}\n",
        pace::report(&snap.pace, snap.now_epoch)
    ));
    if let Some(via) = &snap.via {
        out.push_str(&format!("            via {via}\n"));
    }
    out.push('\n');
    let spread: Vec<String> = snap
        .verdicts
        .by_outcome
        .iter()
        .map(|(name, count)| format!("{name} {}", ui::thousands(*count)))
        .collect();
    out.push_str(&format!(
        "work        {} candidates judged   {}\n            published {}   baseline failed {}\n\n",
        ui::thousands(snap.verdicts.total),
        spread.join("   "),
        snap.published,
        snap.baseline_failed
    ));
    let live = match &snap.live {
        Some(counts) => counts
            .iter()
            .map(|(name, count)| format!("{name} {count}"))
            .collect::<Vec<_>>()
            .join("   "),
        None => "could not ask -- the container refused an exec".to_string(),
    };
    out.push_str(&format!(
        "machine     cpu {:.0} of {} %   mem {} of {}   shm {:.0} of {:.0} GiB\n            live {}\n\n",
        snap.load.cpu_percent,
        snap.cores * 100,
        snap.load.mem_used,
        snap.load.mem_limit,
        snap.shm_used,
        snap.shm_total,
        live
    ));
    out.push_str(&format!(
        "health      pass bugs {}   tracebacks {}\n",
        snap.pass_bugs, snap.tracebacks
    ));
    if let Some(trace) = &snap.traceback {
        out.push('\n');
        out.push_str(trace);
        out.push('\n');
    }
    out
}

fn nothing_to_watch() -> i32 {
    match docker::last_corpse() {
        Some((id, state)) => eprintln!("no reduction is running; the last one ({id}) {state}"),
        None => eprintln!("no reduction is running and none has run"),
    }
    1
}

fn main() -> io::Result<()> {
    // Plain output when there is no terminal to take over, and not only when
    // asked. A TUI needs a tty to initialise and dies without one -- as
    // `failed to initialize terminal: Os { code: 6 }` -- which is what an agent,
    // a pipe and a log file all get. The right answer to "no terminal" is the
    // text screen, not a panic.
    let once = std::env::args().any(|a| a == "--once") || !io::IsTerminal::is_terminal(&io::stdout());
    let interval = Duration::from_secs(
        std::env::args()
            .skip_while(|a| a != "--interval")
            .nth(1)
            .and_then(|n| n.parse().ok())
            .unwrap_or(10),
    );

    if once {
        let Some(snap) = gather(None, None) else {
            std::process::exit(nothing_to_watch());
        };
        print!("{}", plain(&snap));
        return Ok(());
    }

    // Gathering walks the tree and talks to docker, which takes seconds. It
    // runs in its own thread so that the screen still answers a keypress while
    // it does -- a monitor that cannot be quit while it is measuring is worse
    // than no monitor.
    let (tx, rx) = mpsc::channel::<Snapshot>();
    let (quit_tx, quit_rx) = mpsc::channel::<()>();
    std::thread::spawn(move || {
        let mut previous: Option<Snapshot> = None;
        let mut last = Instant::now();
        loop {
            if quit_rx.try_recv().is_ok() {
                return;
            }
            let elapsed = previous.as_ref().map(|_| last.elapsed().as_secs_f64());
            if let Some(snap) = gather(previous.as_ref(), elapsed) {
                last = Instant::now();
                previous = Some(snap.clone());
                if tx.send(snap).is_err() {
                    return;
                }
            } else if tx.send(Snapshot::gone()).is_err() {
                return;
            }
            std::thread::sleep(interval);
        }
    });

    let mut terminal = ratatui::init();
    let mut current: Option<Snapshot> = None;
    let outcome = loop {
        while let Ok(snap) = rx.try_recv() {
            if snap.status == "gone" {
                break;
            }
            current = Some(snap);
        }
        if let Some(snap) = &current {
            terminal.draw(|frame| ui::draw(frame, snap))?;
        }
        if event::poll(Duration::from_millis(200))? {
            if let Event::Key(key) = event::read()? {
                if key.kind == KeyEventKind::Press
                    && matches!(key.code, KeyCode::Char('q') | KeyCode::Esc)
                {
                    break 0;
                }
            }
        }
        if current.is_none() && docker::reduction().is_none() {
            break 1;
        }
    };
    let _ = quit_tx.send(());
    ratatui::restore();
    if outcome != 0 {
        std::process::exit(nothing_to_watch());
    }
    Ok(())
}

impl Snapshot {
    /// How full the container's memory ceiling is, from the two figures docker
    /// prints as text. Parsed rather than recomputed, so the screen cannot
    /// disagree with `docker stats` about the same container.
    pub fn mem_ratio(&self) -> f64 {
        let value = |text: &str| -> f64 {
            let digits: String = text
                .chars()
                .take_while(|c| c.is_ascii_digit() || *c == '.')
                .collect();
            let scale = if text.contains("GiB") {
                1.0
            } else if text.contains("MiB") {
                1.0 / 1024.0
            } else if text.contains("TiB") {
                1024.0
            } else {
                0.0
            };
            digits.parse::<f64>().unwrap_or(0.0) * scale
        };
        let limit = value(&self.load.mem_limit);
        if limit <= 0.0 {
            0.0
        } else {
            value(&self.load.mem_used) / limit
        }
    }

    fn gone() -> Self {
        Snapshot {
            worktree_name: String::new(),
            status: "gone".into(),
            jobs: String::new(),
            original: String::new(),
            before: tree::Shape::default(),
            now: tree::Shape::default(),
            via: None,
            pace: pace::Pace::default(),
            now_epoch: 0.0,
            verdicts: verdicts::Verdicts::default(),
            rate: None,
            published: 0,
            baseline_failed: 0,
            load: docker::Load::default(),
            cores: 1,
            shm_used: 0.0,
            shm_total: 0.0,
            live: None,
            pass_bugs: 0,
            tracebacks: 0,
            traceback: None,
        }
    }
}
