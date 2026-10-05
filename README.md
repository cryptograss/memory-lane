# Memory Lane

A persistent memory system for LLM agents. Currently supported the claude JSONL log format, but can easily support others.  

Memory Lane captures, stores, and provides access to conversation history, enabling continuity across sessions.

## About

This system was developed for [magent](https://github.com/magent-cryptograss/magenta), an AI agent working with the cryptograss team. It enables magent to maintain memory across context windows and sessions by archiving conversations to PostgreSQL and providing access via MCP (Model Context Protocol).

The architecture supports:
- **Eras** - Major phases of work/relationship
- **Context Heaps** - Groups of messages within a context window
- **Messages** - Individual conversation turns (thoughts, tool uses, tool results, etc.)
- **Compacting Actions** - Tracking when context is compacted and summaries generated

## Components

- **Django Web App** (`conversations/`, `memory_viewer/`) - Web interface for viewing and exploring conversation history
- **MCP Server** (`conversations/mcp/`) - Model Context Protocol server for agent memory access
- **Watcher** (`watcher/`) - Monitors Claude Code JSONL files and imports conversations in real-time
- **Importers** (`importers_and_parsers/`) - Parse and import conversation data from various formats
- **Security** (`security/`, `scrubber/`) - Conversation sanitization and secrets filtering

## Quick Start

1. Set up environment:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Configure database (PostgreSQL):
```bash
cp .env.example .env
# Edit .env with your database credentials
```

3. Run migrations:
```bash
python manage.py migrate
```

4. Start the web interface:
```bash
python manage.py runserver 0.0.0.0:4005
```

5. Start the watcher (to import conversations):
```bash
./run_watcher.sh
```

## Docker Deployment

```bash
docker-compose -f docker-compose.services.yml up -d
```

## MCP Server

The memory tools (`magenta-memory-v2`) are an MCP server run from this
repository, deployed on maybelle at `https://mcp.maybelle.cryptograss.live`.
What each tool does, its limits, and how to go from a fragment to the
conversation around it: [docs/MEMORY_TOOLS.md](docs/MEMORY_TOOLS.md).

To run one locally against a database you can reach:

```bash
python manage.py run_mcp_server_v2 --port 8000
claude mcp add --transport http magenta-memory-v2 http://localhost:8000
```

## Management Commands

```bash
# Import Claude Code JSONL conversations
python manage.py import_from_claude_code_v2_jsonl /path/to/file.jsonl

# Repair broken parent chains
python manage.py repair_parent_chains --jsonl-dir ~/.claude/projects/

# Analyze JSONL structure
python manage.py analyze_claude_code_v2_jsonl /path/to/file.jsonl

# Database backup (production backups are a cron on maybelle, not this; see maybelle-config)
python manage.py backup_database

# Redact secrets already in the record (dry run unless --apply)
python manage.py redact_stored
```

## License

MIT
