# Agent Timeline

Agent Timeline is a self-hosted, read-only view of work recorded by AI coding agents. It groups sessions into topic lanes, connects parent and child agents, and shows activity over time. The interface includes time-range controls, search, filters, a concurrency chart, and light and dark themes.

The collector reads transcript metadata and selected session records from your machine, then stores a compact SQLite snapshot. It does not save raw transcript messages or tool output. Job descriptions may be read briefly in memory for topic matching. Optional model-based matching is disabled by default; if enabled, the collector sends only a short, sanitized excerpt to the configured model provider.

Built by Florian Standhartinger — [@airesearch12 on X](https://x.com/airesearch12).

## Point it at your agent logs

Copy `config.example.json` to `config.json` and edit it. `config.json`, `config.local.json`, environment files, and SQLite databases are ignored by Git. Set paths under `paths.transcripts` to the transcript folders used on your machine:

- **Claude Code:** usually `~/.claude/projects` (JSONL files)
- **Codex:** usually `~/.codex/sessions` (JSONL files)
- **OpenCode:** usually `~/.local/share/opencode/storage/session` or `~/.config/opencode/storage/session` (JSON files)

You can point each source at more than one folder. Set `paths.job_roots` if you also want the collector to use local job folders for labels and completion times. Optional OpenCode database, Hermes metadata, systemd/cron journal, and process-tree sources are configured separately. Leave a path empty or disable the source if you do not use it.

Edit the `topics` list to define your own neutral or project-specific lanes. Each item has a `name` and a list of matching `keywords`; the first matching lane wins. Keep local names and keywords in the ignored `config.json` if they should not be published. The example file uses generic topics.

The collector stores timestamps, agent and model names, parent links, topic labels, and source paths. Treat the SQLite database and API as private because local paths and session names can identify your work. Do not put your database, transcript samples, credentials, or screenshots of real activity in a public repository.

## Run locally

Python 3.10 or later is enough for the collector and API; both use the standard library. Copy and configure the example, then collect a snapshot:

```sh
cp config.example.json config.json
python3 collector.py --db "$HOME/.local/share/agent-timeline/agents.sqlite3"
```

Set credentials in your shell or a private environment file, then start the API. It opens the SQLite file in read-only mode and requires a username, password, and independent session-signing secret.

```sh
export AGENT_TIMELINE_USERNAME=admin
export AGENT_TIMELINE_PASSWORD='replace-with-a-long-random-password'
export AGENT_TIMELINE_SESSION_SECRET='replace-with-an-independent-random-secret'
export AGENT_TIMELINE_DB="$HOME/.local/share/agent-timeline/agents.sqlite3"
AGENT_TIMELINE_HOST=127.0.0.1 AGENT_TIMELINE_PORT=8890 python3 server.py
```

In another terminal, build and run the web interface. Nginx serves the static files and proxies API requests to the read-only API:

```sh
docker build -t agent-timeline .
docker run --rm -p 8080:80 \
  -e AGENT_TIMELINE_API_UPSTREAM=http://host.docker.internal:8890 \
  agent-timeline
```

On Linux, add `--add-host=host.docker.internal:host-gateway` to `docker run` if your Docker version does not provide that name. Open `http://localhost:8080`. For production, place the web container behind HTTPS and set `AGENT_TIMELINE_API_UPSTREAM` to a private host address that is reachable only from that container.

The Docker image contains only Nginx and the static interface. The collector, SQLite database, and API stay outside the web container; the API opens the database read-only. Never expose the API port directly to the public internet.

## Configuration

- `AGENT_TIMELINE_CONFIG`: JSON configuration file; defaults to `config.json` beside `collector.py`.
- `AGENT_TIMELINE_HOME`: home directory used to expand `~` in configured paths.
- `AGENT_TIMELINE_DB`: SQLite file path; overrides `database` in the JSON config.
- `AGENT_TIMELINE_USERNAME`, `AGENT_TIMELINE_PASSWORD`, `AGENT_TIMELINE_SESSION_SECRET`: API login and signing values.
- `AGENT_TIMELINE_HOST`, `AGENT_TIMELINE_PORT`: API bind address and port.
- `AGENT_TIMELINE_API_UPSTREAM`: private API URL Nginx proxies to.

Optional model-based classification is off unless `classifier.model` is set in the config. Provide its key through the configured environment variable or private key file. Review the provider and data sent before enabling it.

## Privacy and access

The API only reads the SQLite snapshot. Login cookies are HTTP-only, secure, and same-site; login attempts are rate-limited. Responses include `noindex` headers and the site has a disallowing `robots.txt`. Keep the app behind HTTPS. This is a single-user private dashboard, not a public activity feed or multi-user service.

## License

MIT. See [LICENSE](LICENSE).
