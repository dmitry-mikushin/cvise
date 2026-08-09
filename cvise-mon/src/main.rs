//! cvise-mon -- what the running reduction has actually removed.
//!
//! Run it with no arguments while a reduction is going; it finds the container
//! itself. `--once` prints one plain-text screen instead of taking over the
//! terminal, which is what a script or a log wants.

use std::io;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyCode, KeyEventKind};

use cvise_mon::{docker, gather, nothing_to_watch, plain, ui, Snapshot};

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

