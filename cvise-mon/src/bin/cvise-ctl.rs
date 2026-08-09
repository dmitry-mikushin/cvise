//! cvise-ctl -- end a reduction on purpose.
//!
//! A reduction stops by itself when it has been silent long enough (see
//! cvise/utils/pace.py), and that is the common case. This is for the other
//! one: the result is already good enough, or going on is known to be
//! pointless, and somebody -- often an agent with no terminal to press Ctrl-C
//! in -- has to say so.
//!
//!     cvise-ctl status      what is running, in one line
//!     cvise-ctl stop        stop it, then verify and preserve what it made
//!     cvise-ctl stop --now  stop it and leave the result where it is
//!
//! Stopping is safe by construction and that is worth stating plainly, because
//! it is the thing that makes anyone hesitate. C-Vise rewrites the worktree in
//! place the moment a smaller interesting variant is found, so the tree on disk
//! is the answer at every instant. Nothing is buffered and nothing is lost --
//! the most a stop can cost is the candidates in flight, which are minutes.
//!
//! What a stop CAN cost is the result itself, and not through the reduction:
//! the worktree is in /dev/shm, which does not survive a reboot. So `stop`
//! verifies and preserves by default. `--now` is there for when the tree is
//! known to be broken and only the machine is wanted back.

use std::path::Path;
use std::process::Command;
use std::time::{Duration, Instant};

use cvise_mon::{docker, nothing_to_watch, pace};

/// How long C-Vise is given to finish after SIGTERM, before docker sends
/// SIGKILL.
///
/// Generous on purpose. C-Vise handles termination by deferring it to the next
/// checkpoint in its job loop rather than raising at an arbitrary point (see
/// cvise/utils/sigmonitor.py), so it stops between jobs, not inside one -- and
/// then it still has to wind down a worker pool and write back what it holds.
/// Docker's default of 10 s would turn an orderly shutdown into a SIGKILL for
/// no reason. Waiting is free: this is the last thing the run will ever do.
const GRACE: u64 = 180;

fn verifier() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("verify-reduction.py")
}

/// One line about the live run, cheap enough to poll.
///
/// Cheap is the point, and it is why this is not `cvise-mon --once`: that walks
/// every source file and parses it to count definitions, which is seconds. This
/// reads a container listing and a TSV.
fn status() -> i32 {
    let Some(run) = docker::reduction() else {
        return nothing_to_watch();
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let pace = pace::read(&run.state, now);
    println!(
        "{} {}  {} reductions so far  {}",
        run.id,
        run.status,
        pace.points.len().saturating_sub(1),
        pace::report(&pace, now)
    );
    println!("the tree is {}", run.worktree.display());
    0
}

fn stop(verify: bool) -> i32 {
    let Some(run) = docker::reduction() else {
        return nothing_to_watch();
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let pace = pace::read(&run.state, now);

    // Say what is being ended before ending it. A stop is not reversible, and
    // the number that decides whether it is the right call -- how long since
    // this run last found anything -- is exactly the one nobody has to hand.
    println!("stopping {} ({})", run.id, run.status);
    println!("  {} accepted reductions", pace.points.len().saturating_sub(1));
    println!("  {}", pace::report(&pace, now));
    println!("  the tree is {}", run.worktree.display());

    let started = Instant::now();
    let out = Command::new("docker")
        .args(["stop", "-t", &GRACE.to_string(), &run.id])
        .output();
    match out {
        Ok(out) if out.status.success() => {}
        Ok(out) => {
            eprintln!(
                "docker refused to stop it: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            );
            return 1;
        }
        Err(e) => {
            eprintln!("could not run docker: {e}");
            return 1;
        }
    }
    let took = started.elapsed();
    println!("stopped in {:.0} s", took.as_secs_f64());
    if took >= Duration::from_secs(GRACE) {
        // Worth saying out loud rather than leaving in the timing: it means
        // SIGKILL, so the final write-back and the closing statistics did not
        // happen. The tree is still right -- it is written as the run goes --
        // but if this becomes usual, GRACE is too small and should be measured
        // rather than nudged.
        println!("  it did not finish within {GRACE} s, so docker killed it. The tree \
                  on disk is still what the run had published, but its final \
                  write-back and statistics were lost");
    }

    match wait_for_the_machine() {
        true => println!("the machine is free again"),
        false => println!(
            "the container is stopped, but the machine is not free yet: something \
             still holds {LOCK}. Nothing else will start until it lets go"
        ),
    }

    if !verify {
        println!(
            "not verified, so nothing has been preserved. /dev/shm does not survive a \
             reboot -- when you want to keep this, run:\n    python3 {} {}",
            verifier().display(),
            run.state.display()
        );
        return 0;
    }

    println!("\nverifying and preserving what it made");
    let status = Command::new("python3")
        .arg(verifier())
        .arg(&run.state)
        .status();
    match status {
        Ok(s) if s.success() => 0,
        Ok(s) => {
            // Not an error of the stop, and it must not read like one. The run
            // is down either way; this says the tree it left does not build.
            eprintln!(
                "\nthe run is stopped, but what it published does not verify (exit {}). \
                 The tree is still at {} and the failing build was left behind -- read \
                 that before starting anything else.",
                s.code().unwrap_or(-1),
                run.worktree.display()
            );
            1
        }
        Err(e) => {
            eprintln!("the run is stopped, but the verifier would not run: {e}");
            1
        }
    }
}

/// Where the one-reduction-at-a-time lock lives. cvise-ns-projection.py.
const LOCK: &str = "/dev/shm/cvise.lock";

/// Wait until the next run could actually start, and say whether it can.
///
/// Not the same question as "did the container stop", which is what this used
/// to answer. MEASURED after a stop: `docker stop` returned in 17 s, and the
/// `docker run --rm` client the driver is waiting on lived for minutes after
/// that, cleaning up. The driver holds the lock until that client exits, so
/// "the machine is free again" was printed while the next run would still have
/// been refused.
///
/// Asked of the lock itself rather than of the process list, because the lock
/// is what the next run will actually contend for. flock(1) takes it and drops
/// it in the same breath; taking it here would be a race against a run that is
/// entitled to start the moment we let go.
fn wait_for_the_machine() -> bool {
    let deadline = Instant::now() + Duration::from_secs(GRACE);
    loop {
        let free = Command::new("flock")
            .args(["-n", LOCK, "true"])
            .status()
            .map(|s| s.success())
            .unwrap_or(true);
        if free {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_secs(1));
    }
}

fn usage() -> i32 {
    eprintln!(
        "cvise-ctl -- end a reduction on purpose\n\
         \n\
             cvise-ctl status        what is running, in one line\n\
             cvise-ctl stop          stop it, then verify and preserve what it made\n\
             cvise-ctl stop --now    stop it and leave the result where it is\n\
         \n\
         Stopping loses nothing: C-Vise rewrites the worktree in place as it goes,\n\
         so the tree on disk is the answer at every instant, and `--resume` on it\n\
         continues from there."
    );
    2
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match args.first().map(String::as_str) {
        Some("status") => status(),
        Some("stop") => stop(!args.iter().any(|a| a == "--now")),
        _ => usage(),
    };
    std::process::exit(code);
}
