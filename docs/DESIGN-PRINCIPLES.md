# Design principles for Dibs

Source: Apple Human Interface Guidelines, "Design principles" (reintroduced June 8, 2026).
Dibs follows these. Each principle below carries Apple's one-line meaning and what it
means concretely for this product. Use this as the checklist for every human-facing surface:
dashboard, overlay, consent prompt, tray icon, CLI output, README, and error messages agents relay.

## Purpose — make something meaningful
Apple: identify what matters most to the people you're designing for and make those things great.
For Dibs:
- The person who matters is the human at the machine, not the agents. Every surface answers one
  question first: **who has the desk right now, and how do I take it back?**
- The core loop is: an agent asks, the human allows or ignores, the agent works visibly, the human
  can take over instantly. Anything that doesn't serve that loop is optional.
- Don't re-create Task Manager or a log viewer. The audit tail exists to answer "what did it just do?",
  not to be a database browser.

## Agency — let people do things their own way
Apple: stay out of the way, give freedom to explore, help people recover from mistakes.
For Dibs:
- Touching the mouse or keyboard always wins, instantly, with no dialog. Agents pause and lose the desk.
- Never force a flow on the human: the consent prompt can be ignored (it times out to deny), dismissed,
  or answered from the prompt, the tray, the dashboard, or a hotkey. Same action, any door.
- Every state is reversible from the same place it was set: Pause/Resume, mode switch, revoke, force
  release. Auto-resume after idle so a forgotten pause doesn't strand the agents.
- Looking is not free: a screenshot of the human's screen is a privacy act. Every action except a plain
  wait needs dibs, and in Ask mode dibs come only from the human saying yes (never from them being idle).

## Responsibility — act in people's best interest
Apple: be transparent about what the product does and why; keep information safe.
For Dibs:
- Nothing moves on the desktop without being visible: halo on the cursor, banner naming the agent and its
  purpose, click flashes, typing tag. If it isn't shown, it doesn't happen.
- Consent prompts state **who** wants the desk and **why** (the agent's registered purpose), and how long
  the grant lasts. No vague "allow access?".
- Every action is logged with the agent id; screenshots are kept locally in `data/`, never uploaded by Dibs.
- Dangerous capabilities default off (`allow_launch`), remote access defaults off (loopback bind), tokens
  are shown once.
- Agents may never author their own consent: decisions come only from human surfaces (prompt, tray,
  dashboard, hotkey), never from the REST/MCP agent routes.

## Familiarity — build on what people know
Apple: use known concepts, keep visuals and interactions consistent, provide clear feedback.
For Dibs:
- "Dibs" is the metaphor everywhere: an agent *has dibs*, *calls dibs*, *loses dibs*. Don't mix in
  "lease", "lock", or "mutex" in human-facing copy (the API keeps `lease` for engineers).
- Traffic-light states, the same colours in every surface: cyan = an agent has dibs, green = you have the
  desk, amber = someone is asking, red = paused, grey = idle. Tray icon, banner, dashboard pill, and
  consent card all use the same five.
- Windows conventions: tray icon with a right-click menu and left-click default action, toast for a
  request, a standard-looking hotkey chord, Scheduled Task for autostart.
- Feedback for every action: a button press changes the pill within a second; a hotkey shows a banner
  line; a denial tells the agent why and when to retry.

## Flexibility — adapt to diverse contexts and needs
Apple: design for everyone, preserve context, consider many input methods, treat each platform with intention.
For Dibs:
- Four ways to decide anything: on-screen prompt (mouse), hotkey (keyboard), tray (mouse), dashboard
  (any device on the network, including a phone). None is required.
- Two monitors are normal here; the overlay spans all of them and the banner sits on the primary.
- Modes fit different days: `ask` when the user is working, `hands_off` when they step away, `locked` when
  nothing may touch the machine.
- Colour is never the only signal: every state also has a word (PAUSED, "you have the desk", the agent's name).
- The dashboard works at phone width; the overlay is readable at 100% and 150% DPI.

## Simplicity — be clear and direct
Apple: include just what's necessary, be concise, establish hierarchy.
For Dibs:
- Dashboard hierarchy, top to bottom: status pill + Pause · consent card (only when pending) · who has
  the desk · agents · audit. Stats and config are quiet.
- One accent colour plus the state colours. No decoration that isn't a state.
- Copy is short and plain: "claude-code has dibs", "gemini wants the desk", "You have the desk — agents
  paused". No jargon in anything a human reads on screen.
- The banner is one line plus one hint line. The consent prompt is name, purpose, countdown, two buttons.

## Craft — care about every detail
Apple: quality sets the tone; experiment and iterate; maintain it.
For Dibs:
- Overlay never flickers, never steals focus, never trips Do Not Disturb, never stays on screen after the
  server stops.
- Countdowns are live and accurate; relative times ("12 s ago") tick.
- Icons read at 16 px; text is legible over light and dark backgrounds.
- Tests run the real surfaces on the real desktop (`-m display`) before a change ships.

## Delight — make it human
Apple: pick the emotion, create defining moments, don't mistake delight for decoration, consider the whole.
For Dibs:
- The emotion is **calm confidence**: you always know who's driving, and you can always take the wheel.
- Defining moments: the halo appearing around the cursor when an agent starts; the green "You have the
  desk" flash when you grab the mouse; a toast that reads like a polite colleague asking.
- Whimsy is welcome, in service of the state: a soft breathing glow on the cursor and along the screen edges
  while an agent has dibs, mouse motion that moves like a hand rather than a teleport, a friendly status
  line ("All yours"), one small sheep in the empty state. Still: no decoration without a state behind it,
  no animation that outlasts its purpose, and `prefers-reduced-motion` turns the motion off. (9/4)
