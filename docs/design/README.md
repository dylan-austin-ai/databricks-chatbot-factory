# UI design (Claude Design canvas)

The source of the app's look: seventeen screens (Home, the create flow (path choice, fast path, six advanced steps), Documents, Test questions,
Launch, Chat, Monitoring, Traces, All chatbots, Admin) as `.dc.html` artboards plus `canvas.json` (layout). They render in the
Claude Design canvas, which supplies their runtime (`support.js`); opened directly in a browser
they show unstyled markup. Edit the live canvas, then copy the files back here.

The Streamlit app follows them through `app/ui.py` (colors, type, pills, tiles, stepper) and
`.streamlit/config.toml` (theme). Screen copy and behavior come from the UI requirements document, which is kept outside this repository.
