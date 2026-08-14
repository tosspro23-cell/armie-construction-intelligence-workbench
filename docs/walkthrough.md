# Local walkthrough

1. Start Ollama, pull `qwen3:8b` and `qwen3-vl:8b`, then start the API and Vite UI.
2. Open the printed local URL and confirm the BIM Model loads with synthetic elements.
3. Ask `How many doors are in the model?` and expand the Decision Summary.
4. Ask `Which level contains the most windows?` and inspect the typed plan, IFC evidence, and verification.
5. Ask `Find the tallest door.` and open the winner citation.
6. Switch to Engineering Drawing and ask `What is the diversity factor for Panel-A?`.
7. Capture the viewer and ask `Is the target element clearly visible?`; accept clarification when the view is insufficient.
8. Ask an unsupported cross-source question and confirm no partial numeric answer is produced.

The walkthrough uses only synthetic public data. Do not record or commit local runtime traces or screenshots that contain private source material.
