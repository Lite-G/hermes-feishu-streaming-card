# Native tables / #391 acceptance

- Source: PR #391 `18cd440dbe5c` plus the v4.7.7 lossless fallback changes.
- Failing reproduction: 18 failures before the fix in the expanded native-table tests (long header/data, Chinese/ASCII, default/task and mixed tables). These are rendering tests, not platform evidence.
- Focused implementation checks: 499 passed across native tables, text, render, presentation, config and reading-preset HTTP tests. Release/docs/server batch: 782 passed and one newline-normalization expectation corrected; the targeted handoff rerun passed both cases. Full cross-platform CI remains required before publishing.
- Real Feishu API: synthetic mixed native/Markdown card sent only to the existing dedicated test group; CardKit update succeeded. A separate 5,000-character Markdown header card entity was accepted without sending another message.
- Real desktop: native header/rows and surrounding prose were readable in the current light theme. The long Markdown cell had an internal scrollbar; scrolling to its end visibly showed `TAIL_391_OK`, followed by the outside `END_391_OK` marker. Source payload retained the entire cell.
- Scope: this proves the direct card-render/CardKit/display path, not a new Gateway/model round trip. Android/iOS, dark theme and narrow-window coverage are not implied. Test messages are recalled after inspection; private identifiers and credentials are not stored in repository evidence.
