//! The rhythm of the run, read from the series C-Vise writes.
//!
//! Not scraped from `docker logs`. The progress line there carries a relative
//! clock, so three consecutive runs in one log are indistinguishable from one,
//! and the elapsed times cannot be compared across a restart. The TSV beside
//! the verdict journal carries a wall-clock timestamp per accepted reduction,
//! which is what any of this needs.
//!
//! The thresholds below must agree with cvise/utils/pace.py. They are duplicated
//! rather than shared because this is a different program in a different
//! language, and the split is deliberate: the DECISION to stop is C-Vise's
//! alone, and this only displays. If these drift, a number on a screen is wrong
//! and nothing else is.

use std::path::Path;

/// Times the usual interval that silence must exceed. cvise/utils/pace.py.
pub const PATIENCE: f64 = 8.0;
/// Intervals needed before their median means anything. cvise/utils/pace.py.
pub const WARMUP: usize = 5;
/// The band quoted with the forecast: 98% of observed intervals fell inside it.
pub const BAND: f64 = 3.0;

#[derive(Clone, Debug, Default)]
pub struct Point {
    pub when: f64,
    pub lines: usize,
    pub via: String,
}

#[derive(Clone, Debug, Default)]
pub struct Pace {
    pub points: Vec<Point>,
    /// Intervals between accepted reductions, in seconds, oldest first.
    pub gaps: Vec<f64>,
}

impl Pace {
    /// The interval to expect, or None while the run is too young to say.
    pub fn usual(&self) -> Option<f64> {
        if self.gaps.len() < WARMUP {
            return None;
        }
        let mut sorted = self.gaps.clone();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let middle = sorted.len() / 2;
        Some(if sorted.len() % 2 == 0 {
            (sorted[middle - 1] + sorted[middle]) / 2.0
        } else {
            sorted[middle]
        })
    }

    pub fn last(&self) -> Option<&Point> {
        self.points.last()
    }

    /// Seconds since the last accepted reduction, by the wall clock.
    ///
    /// Against `now` and not against the newest point's own age, because the
    /// question is how long it has been quiet, and a run that died an hour ago
    /// must not read as busy.
    pub fn silence(&self, now: f64) -> Option<f64> {
        self.last().map(|p| (now - p.when).max(0.0))
    }

    /// When C-Vise will give up, unless something arrives first.
    pub fn deadline(&self) -> Option<f64> {
        Some(self.last()?.when + PATIENCE * self.usual()?)
    }

    /// Lines removed per hour, over the whole series.
    ///
    /// Growth is counted as zero rather than as negative: several passes make
    /// the text bigger on the way to making it smaller, and a rate that dips
    /// below zero when one lands says nothing about how the run is going.
    pub fn per_hour(&self, now: f64) -> Option<f64> {
        // To `now`, not to the newest point. Measured to the newest point the
        // rate freezes the moment a run goes quiet, which is exactly when a
        // falling rate is the thing worth seeing.
        let seconds = now - self.points.first()?.when;
        if seconds < 60.0 {
            return None;
        }
        let removed: usize = self
            .points
            .windows(2)
            .map(|w| w[0].lines.saturating_sub(w[1].lines))
            .sum();
        Some(removed as f64 / (seconds / 3600.0))
    }

    /// The newest pass combination that produced anything.
    pub fn via(&self) -> Option<String> {
        self.points
            .iter()
            .rev()
            .find(|p| !p.via.is_empty())
            .map(|p| p.via.clone())
    }
}

/// Read the series of the live run.
///
/// The file is appended to across runs that share a state directory, so the
/// earlier ones have to be cut off or the live run's pace is set by a run that
/// ended yesterday. C-Vise writes a `#run` line where each begins, so this is a
/// reading rather than a guess -- and guessing would mean guessing by the size
/// of the interval, where the longest gap ever observed WITHIN a run is 66 min.
pub fn read(state: &Path, now: f64) -> Pace {
    let path = state.join("tmp/cvise-progress.tsv");
    let Ok(text) = std::fs::read_to_string(&path) else {
        return Pace::default();
    };

    let mut points: Vec<Point> = Vec::new();
    for line in text.lines() {
        if line.starts_with("#run") {
            points.clear();
            continue;
        }
        if line.starts_with('#') {
            continue;
        }
        let mut fields = line.split('\t');
        let when = fields.next().and_then(|f| f.parse::<f64>().ok());
        let _bytes = fields.next();
        let lines = fields.next().and_then(|f| f.parse::<usize>().ok());
        let _files = fields.next();
        let via = fields.next().unwrap_or("").trim().to_string();
        if let (Some(when), Some(lines)) = (when, lines) {
            if when > now + 60.0 {
                // A clock that is ahead of this machine's is a series from
                // somewhere else, and averaging it in would be inventing a run.
                continue;
            }
            points.push(Point { when, lines, via });
        }
    }

    let gaps = points
        .windows(2)
        .map(|w| w[1].when - w[0].when)
        .collect();
    Pace { points, gaps }
}

/// A duration as a person reads one.
pub fn clock(seconds: f64) -> String {
    let seconds = seconds.max(0.0) as u64;
    if seconds >= 3600 {
        format!("{}h{:02}m", seconds / 3600, seconds % 3600 / 60)
    } else {
        format!("{}m{:02}s", seconds / 60, seconds % 60)
    }
}

/// What to put on the screen, saying only what is known.
pub fn report(pace: &Pace, now: f64) -> String {
    let Some(usual) = pace.usual() else {
        return format!(
            "{} of {WARMUP} intervals needed before this run can say how fast it is going",
            pace.gaps.len()
        );
    };
    // Word for word what cvise/utils/pace.py logs, so that a person moving
    // between this screen and the run's own output is reading one thing.
    let mut parts = vec![format!("a reduction every {}", clock(usual))];
    if let Some(rate) = pace.per_hour(now) {
        parts.push(format!("{rate:.0} lines/h"));
    }
    if let (Some(deadline), Some(silence)) = (pace.deadline(), pace.silence(now)) {
        let left = deadline - now;
        if left > 0.0 {
            // Only while there is still a run to forecast for: "next one due
            // within 32m" beside "silent for 3h" is two statements that cannot
            // both be true, and the reader believes the reassuring one.
            parts.push(format!("next one due within {}", clock(BAND * usual)));
            parts.push(format!("giving up in {}", clock(left)));
        } else {
            parts.push(format!("silent for {}", clock(silence)));
            parts.push("giving up now".to_string());
        }
    }
    parts.join("; ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    /// The real intervals of the run that spent 6.0 h of its 10.96 h after its
    /// last accepted reduction, and of one that was still working.
    const DEAD: &[u64] = &[
        423, 556, 785, 412, 592, 588, 923, 927, 1265, 617, 870, 762, 1021, 1129, 685, 733, 1377,
        1098, 822, 1038, 108, 86, 106, 81, 317, 136, 39, 174, 32, 33,
    ];
    const HEALTHY: &[u64] = &[
        708, 806, 1240, 832, 905, 880, 1291, 646, 775, 712, 629, 512, 692, 619, 814, 716, 776,
        1561, 1397, 1322, 1236,
    ];

    fn written(dir: &Path, gaps: &[u64], start: f64) -> std::path::PathBuf {
        let tmp = dir.join("tmp");
        std::fs::create_dir_all(&tmp).unwrap();
        let path = tmp.join("cvise-progress.tsv");
        let mut f = std::fs::File::create(&path).unwrap();
        writeln!(f, "#when\tbytes\tlines\tfiles\tvia").unwrap();
        writeln!(f, "#run\t{start:.0}\t1234").unwrap();
        let mut at = start;
        let mut lines = 100_000usize;
        writeln!(f, "{at:.0}\t1000\t{lines}\t10\t").unwrap();
        for gap in gaps {
            at += *gap as f64;
            lines -= 100;
            writeln!(f, "{at:.0}\t1000\t{lines}\t10\tLinesPass::0").unwrap();
        }
        path
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("cvise-mon-pace-{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_run_that_has_gone_quiet_is_past_its_deadline() {
        let dir = scratch("dead");
        let total: u64 = DEAD.iter().sum();
        let now = 2_000_000.0;
        written(&dir, DEAD, now - total as f64 - 21_610.0);
        let pace = read(&dir, now);
        assert_eq!(pace.points.len(), DEAD.len() + 1);
        assert!(pace.deadline().unwrap() < now, "not called finished");
        assert!(report(&pace, now).contains("silent for"));
    }

    #[test]
    fn a_run_still_working_has_time_left() {
        let dir = scratch("healthy");
        let total: u64 = HEALTHY.iter().sum();
        let now = 2_000_000.0;
        written(&dir, HEALTHY, now - total as f64 - 514.0);
        let pace = read(&dir, now);
        assert!(pace.deadline().unwrap() > now, "a working run was called over");
        assert!(report(&pace, now).contains("giving up in"));
    }

    #[test]
    fn a_run_too_young_refuses_to_guess() {
        let dir = scratch("young");
        written(&dir, &DEAD[..2], 1_999_000.0);
        let pace = read(&dir, 2_000_000.0);
        assert!(pace.usual().is_none());
        assert!(pace.deadline().is_none());
        assert!(report(&pace, 2_000_000.0).contains("intervals needed"));
    }

    #[test]
    fn an_earlier_run_in_the_same_file_is_not_averaged_in() {
        // A dead run's points, then a gap of hours, then the live one's. Taking
        // the lot would put the live run's pace at the mercy of a run that
        // ended yesterday.
        let dir = scratch("two-runs");
        let now = 2_000_000.0;
        let total: u64 = HEALTHY.iter().sum();
        let path = written(&dir, HEALTHY, now - total as f64 - 514.0);
        let old = written(&scratch("two-runs-old"), DEAD, 1_000_000.0);
        let mut both = std::fs::read_to_string(&old).unwrap();
        both.push_str(&std::fs::read_to_string(&path).unwrap());
        std::fs::write(&path, both).unwrap();

        let pace = read(&dir, now);
        assert_eq!(pace.points.len(), HEALTHY.len() + 1, "the old run leaked in");
        assert!(pace.deadline().unwrap() > now);
    }

    #[test]
    fn no_file_is_not_a_reason_to_invent_numbers() {
        let pace = read(&scratch("empty"), 2_000_000.0);
        assert!(pace.usual().is_none());
        assert!(pace.per_hour(2_000_000.0).is_none());
        assert!(pace.via().is_none());
    }

    #[test]
    fn growth_does_not_make_the_rate_negative() {
        let dir = scratch("growth");
        let tmp = dir.join("tmp");
        std::fs::create_dir_all(&tmp).unwrap();
        std::fs::write(
            tmp.join("cvise-progress.tsv"),
            "#when\tbytes\tlines\tfiles\tvia\n\
             #run\t1000\t1234\n\
             1000\t100\t50\t5\t\n\
             1600\t120\t60\t5\tInlinePass\n",
        )
        .unwrap();
        assert_eq!(read(&dir, 2000.0).per_hour(2000.0), Some(0.0));
    }
}

#[cfg(test)]
mod agreement {
    //! The same rule, in two languages, asked of the same real series.
    //!
    //! The decision to stop is C-Vise's and this only displays -- but it
    //! displays the same quantities computed a second time, and two copies of a
    //! rule drift. The fixture is run16's real intervals and the answers were
    //! computed by cvise/utils/pace.py; cvise/tests/test_pace.py checks the
    //! same file from the other side, so drift on either fails a test.

    use super::*;

    fn data(name: &str) -> String {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("cvise/tests/data")
            .join(name);
        std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("{}: {e}", path.display()))
    }

    #[test]
    fn it_says_what_the_python_says() {
        let dir = std::env::temp_dir().join("cvise-mon-agreement");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("tmp")).unwrap();
        std::fs::write(
            dir.join("tmp/cvise-progress.tsv"),
            data("run16-progress.tsv"),
        )
        .unwrap();

        let expected = data("run16-expected.tsv");
        let mut checked = 0;
        for line in expected.lines().filter(|l| !l.starts_with('#')) {
            let f: Vec<&str> = line.split('\t').collect();
            let now: f64 = f[0].parse().unwrap();
            let usual: f64 = f[1].parse().unwrap();
            let deadline: f64 = f[2].parse().unwrap();
            let spent = f[3] == "true";

            let pace = read(&dir, now);
            assert!(
                (pace.usual().unwrap() - usual).abs() < 0.001,
                "usual at {now}: {:?} not {usual}",
                pace.usual()
            );
            assert!(
                (pace.deadline().unwrap() - deadline).abs() < 1.0,
                "deadline at {now}: {:?} not {deadline}",
                pace.deadline()
            );
            assert_eq!(
                pace.deadline().unwrap() <= now,
                spent,
                "the verdict at {now} differs"
            );
            // The rendered line too, not only the numbers behind it. Two
            // screens that agree on the arithmetic and disagree on the words
            // are still two different answers to the person reading them.
            assert_eq!(report(&pace, now), f[4], "the line at {now} differs");
            checked += 1;
        }
        assert!(checked >= 8, "the fixture stopped covering both sides of the deadline");
    }

    #[test]
    fn only_the_last_run_in_the_file_counts() {
        // The fixture holds three runs. Reading the lot would put the live
        // run's pace at the mercy of ones that ended long ago.
        let dir = std::env::temp_dir().join("cvise-mon-agreement-runs");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("tmp")).unwrap();
        let series = data("run16-progress.tsv");
        std::fs::write(dir.join("tmp/cvise-progress.tsv"), &series).unwrap();

        let all_points = series
            .lines()
            .filter(|l| !l.starts_with('#'))
            .count();
        let runs = series.lines().filter(|l| l.starts_with("#run")).count();
        assert!(runs >= 3, "the fixture no longer holds several runs");

        let pace = read(&dir, 1_786_200_000.0);
        assert!(pace.points.len() < all_points, "every run was averaged in");
    }
}
