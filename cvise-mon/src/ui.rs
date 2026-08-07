//! The screen.
//!
//! Three bars carry the answer and they are the first thing on it: what
//! fraction of the files, of the lines, and of the functions is gone. Bars are
//! drawn here rather than with a Gauge widget because a Gauge centres its label
//! inside the bar, and a label long enough to carry both numbers then sits
//! across the boundary between what is gone and what is left -- which is the
//! one boundary the reader is looking for.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::Frame;

use crate::Snapshot;

const GONE: Color = Color::Green;
const LEFT: Color = Color::DarkGray;
const DIM: Color = Color::Gray;
// The pace line, in the two states it has: still finding things, or out of time.
const GOOD: Color = Color::Green;
const WARN: Color = Color::Yellow;
const BAR: usize = 28;

pub fn thousands(value: usize) -> String {
    let digits = value.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(' ');
        }
        out.push(c);
    }
    out
}

/// A bar as two spans, so the boundary is a colour change and not a character
/// the eye has to find.
fn bar(ratio: f64, filled: Color) -> Vec<Span<'static>> {
    let done = (ratio.clamp(0.0, 1.0) * BAR as f64).round() as usize;
    vec![
        Span::styled("█".repeat(done), Style::default().fg(filled)),
        Span::styled("░".repeat(BAR - done), Style::default().fg(LEFT)),
    ]
}

fn removed_row(label: &str, now: usize, before: usize) -> Line<'static> {
    let removed = before.saturating_sub(now);
    let ratio = if before == 0 {
        0.0
    } else {
        removed as f64 / before as f64
    };
    let mut spans = vec![Span::styled(
        format!(" {label:<10}"),
        Style::default().add_modifier(Modifier::BOLD),
    )];
    spans.extend(bar(ratio, GONE));
    spans.push(Span::raw(format!(
        "  {:>9} left of {:<9}",
        thousands(now),
        thousands(before)
    )));
    spans.push(Span::styled(
        format!("  -{:<9} {:>5.1}% gone", thousands(removed), ratio * 100.0),
        Style::default().fg(GONE),
    ));
    Line::from(spans)
}

fn plain_row(label: &str, ratio: f64, colour: Color, text: String) -> Line<'static> {
    let mut spans = vec![Span::raw(format!(" {label:<10}"))];
    spans.extend(bar(ratio, colour));
    spans.push(Span::styled(format!("  {text}"), Style::default().fg(DIM)));
    Line::from(spans)
}

fn titled(title: String) -> Block<'static> {
    Block::default()
        .borders(Borders::ALL)
        .title(title)
        .title_style(Style::default().add_modifier(Modifier::BOLD))
}

pub fn draw(frame: &mut Frame, snap: &Snapshot) {
    // Health takes the rest of the screen only when it has a traceback to put
    // there. Given the space unconditionally it reads as a panel that failed to
    // load, which is the opposite of what a health panel should say.
    let health_height = match &snap.traceback {
        Some(_) => Constraint::Min(4),
        None => Constraint::Length(3),
    };
    let root = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(7),
            Constraint::Length(5),
            Constraint::Length(6),
            health_height,
            Constraint::Min(0),
        ])
        .split(frame.area());

    reduced(frame, root[0], snap);
    work(frame, root[1], snap);
    machine(frame, root[2], snap);
    health(frame, root[3], snap);
}

fn reduced(frame: &mut Frame, area: Rect, snap: &Snapshot) {
    let block = titled(format!(
        " reduced  {}  {}  N={} ",
        snap.worktree_name, snap.status, snap.jobs
    ));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    if snap.before.files == 0 {
        frame.render_widget(
            Paragraph::new(" measuring the original tree, once...")
                .style(Style::default().fg(DIM)),
            inner,
        );
        return;
    }

    let mut text = vec![
        removed_row("files", snap.now.files, snap.before.files),
        removed_row("lines", snap.now.lines, snap.before.lines),
        removed_row("functions", snap.now.functions, snap.before.functions),
        Line::from(""),
    ];
    // The line a person is actually waiting for. Above the provenance, because
    // "how much longer" is the question and "which pass" is the footnote.
    let silent = snap
        .pace
        .deadline()
        .map(|d| d <= snap.now_epoch)
        .unwrap_or(false);
    text.push(Line::from(Span::styled(
        format!(" {}", crate::pace::report(&snap.pace, snap.now_epoch)),
        Style::default().fg(if silent { WARN } else { GOOD }),
    )));

    let mut note = format!(" original {}", &snap.original[..12.min(snap.original.len())]);
    if let Some(via) = &snap.via {
        note.push_str(&format!("   via {via}"));
    }
    text.push(Line::from(Span::styled(note, Style::default().fg(DIM))));
    frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), inner);
}

fn work(frame: &mut Frame, area: Rect, snap: &Snapshot) {
    let block = titled(" work ".to_string());
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let rate = match snap.rate {
        Some(rate) => format!("{rate:.2}/s"),
        None => "rate after the next refresh".to_string(),
    };
    let spread: Vec<String> = snap
        .verdicts
        .by_outcome
        .iter()
        .map(|(name, count)| format!("{name} {}", thousands(*count)))
        .collect();
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(format!(
                " {:<10}{:>9} judged   {}",
                "candidates",
                thousands(snap.verdicts.total),
                rate
            )),
            Line::from(format!(" {:<10}{}", "", spread.join("    "))),
            Line::from(Span::styled(
                format!(
                    " {:<10}{} published   {} baseline failures",
                    "", snap.published, snap.baseline_failed
                ),
                Style::default().fg(DIM),
            )),
        ]),
        inner,
    );
}

fn machine(frame: &mut Frame, area: Rect, snap: &Snapshot) {
    let block = titled(" machine ".to_string());
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let ceiling = (snap.cores * 100) as f64;
    let live = match &snap.live {
        Some(counts) => counts
            .iter()
            .map(|(name, count)| format!("{name} {count}"))
            .collect::<Vec<_>>()
            .join("   "),
        // Not "nothing": the container refuses an exec when it is at its memory
        // ceiling, and an empty list there reads as an idle machine.
        None => "could not ask -- the container refused an exec".to_string(),
    };
    frame.render_widget(
        Paragraph::new(vec![
            plain_row(
                "cpu",
                snap.load.cpu_percent / ceiling.max(1.0),
                Color::Cyan,
                format!(
                    "{:.0} of {:.0} %  ({} cores)",
                    snap.load.cpu_percent, ceiling, snap.cores
                ),
            ),
            plain_row(
                "memory",
                snap.mem_ratio(),
                Color::Magenta,
                format!("{} of {}", snap.load.mem_used, snap.load.mem_limit),
            ),
            plain_row(
                "/dev/shm",
                snap.shm_used / snap.shm_total.max(1.0),
                Color::Blue,
                format!("{:.0} of {:.0} GiB", snap.shm_used, snap.shm_total),
            ),
            Line::from(Span::styled(
                format!(" live       {live}"),
                Style::default().fg(DIM),
            )),
        ])
        .wrap(Wrap { trim: false }),
        inner,
    );
}

fn health(frame: &mut Frame, area: Rect, snap: &Snapshot) {
    let hurt = snap.pass_bugs > 0 || snap.tracebacks > 0;
    let block = titled(" health ".to_string()).border_style(if hurt {
        Style::default().fg(Color::Red)
    } else {
        Style::default()
    });
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let mut text = vec![Line::from(Span::styled(
        format!(
            " pass bugs {}   tracebacks {}{}",
            snap.pass_bugs,
            snap.tracebacks,
            if hurt { "" } else { "        q to quit" }
        ),
        Style::default().fg(if hurt { Color::Red } else { Color::Green }),
    ))];
    if let Some(trace) = &snap.traceback {
        for line in trace.lines().take(inner.height.saturating_sub(1) as usize) {
            text.push(Line::from(Span::styled(
                format!(" {line}"),
                Style::default().fg(Color::Red),
            )));
        }
    }
    frame.render_widget(Paragraph::new(text), inner);
}
