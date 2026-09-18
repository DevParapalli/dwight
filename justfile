# Install dependencies
install:
    uv sync

# The agent and the target system are separate processes on purpose: the target
# is a stand-in for a real vendor API, so pushing to it is real HTTP.

# Run the agent on :8000, reloading on edit
dev:
    uv run uvicorn app.main:app --reload

# --enable-nuke matches compose.yaml: POST /nuke empties this store between
# takes. Testing convenience, and the route does not exist without the flag.

# Run the stand-in target system on :8900
target:
    uv run tools/mock_target_api.py --port 8900 --enable-nuke

# Regenerate the three source files and their ground-truth manifest
samples rows="5000":
    uv run tools/generate_sources.py --rows {{rows}} --out data/samples

# A small fixture that still fires every escalation type, so the whole flow runs
# in a couple of minutes instead of the full set's four.

# Reset and generate a smaller fixture for a quick end-to-end run
demo rows="1500":
    just reset
    uv run tools/generate_sources.py --rows {{rows}} --out data/samples
    @echo ""
    @echo "Fixture ready. Then:"
    @echo "  terminal 1:  just target"
    @echo "  terminal 2:  just dev"
    @echo "  browser:     http://127.0.0.1:8000  -> upload all three files in data/samples/"
    @echo "  after push:  just measure"

# Score the last run against the generator's ground-truth manifest.
measure:
    uv run tools/measure_escalations.py --json data/samples/measurement.json

# Typeset with Centauri (the print half of Proxima). Needs the centauri checkout
# beside this one; --root spans both so the import resolves, and its fonts must
# be on the path or page breaks shift.

# Typeset the write-up and deep dive to PDF
docs centauri="../centauri":
    typst compile --root {{justfile_directory()}}/.. \
        --font-path {{centauri}}/fonts \
        docs/deepdive.typ docs/deepdive.pdf
    typst compile --root {{justfile_directory()}}/.. \
        --font-path {{centauri}}/fonts \
        docs/writeup.typ docs/writeup.pdf
    @echo "wrote docs/deepdive.pdf and docs/writeup.pdf"

# Re-typeset the deep dive on every edit
docs-watch centauri="../centauri":
    typst watch --root {{justfile_directory()}}/.. \
        --font-path {{centauri}}/fonts \
        docs/deepdive.typ docs/deepdive.pdf

# Bring up both services in containers
up:
    podman compose up --build

# Stop the containers
down:
    podman compose down

# Keeps the model cache, so the next run does not pay for the same answers
# twice. Needs ENABLE_NUKE=true.

# Clear both stores between takes, keeping the model cache
nuke port="8000":
    curl -fsS -X POST localhost:{{port}}/nuke -H 'accept: application/json'
    @echo ""

# Delete the databases outright, model cache included
reset:
    rm -rf data/dwight.db data/dwight.db-wal data/dwight.db-shm data/runs data/mock_target.db
