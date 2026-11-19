# Data Analysis Agent

A Streamlit application for exploring tabular data with questions written in plain English. The app combines an LLM to interpret a question with Python data tools and SQL to calculate results from the selected dataset. It can also profile and clean data, generate charts, and prepare Power BI measures and dashboard guidance.

**Blinkit grocery sales is the example dataset, not the limit of the application.** Users can upload CSV/Excel files or select a table in a connected MySQL database. The project is an analytics tool; it does not place grocery orders, track deliveries, or connect to live Blinkit sales.

## The problem this project solves

Answering a business question from a spreadsheet often requires knowing the schema, writing SQL or Python, checking data quality, and building a visualization. This project puts those steps behind a conversational interface:

1. A user selects or uploads a dataset.
2. The user asks a question in ordinary language.
3. An LLM interprets the request and chooses an available analysis tool.
4. Python and SQL run the actual profiling, calculations, or chart query.
5. The app presents the answer and, where requested, a chart or Power BI plan.

The LLM is used to understand questions and select tools; it is not intended to invent figures. The agent instructions tell it to retrieve numerical results from SQL, aggregate data in SQL, and state when the dataset cannot answer a question.

## What it can do

- Connect to an existing MySQL table.
- Upload CSV, XLSX, XLS, or XLSM data into a database table (up to 200,000 rows per upload).
- Show row and column counts, inferred column roles, missing values, duplicate rows, numeric summaries, and common label variations.
- Answer questions using one read-only `SELECT` query at a time (the tool returns up to 200 rows).
- Suggest data-appropriate charts and create bar, column, line, donut, or scatter charts.
- Clean a selected table into a **new** table, leaving its source table untouched.
- Generate Power BI DAX measures, suggested cards/slicers/visuals, and Power Query/SQL instructions.
- Create a Power BI-friendly database view and save requested findings to an `agent_insights` table.

What the Power BI feature does **not** do: it does not automate Power BI Desktop or build and publish a `.pbix` report. It prepares measures, a layout plan, and a view that a user can load into Power BI.

## Architecture

```text
                         ┌─────────────────────────────┐
                         │           User              │
                         │ selects data / asks a query │
                         └──────────────┬──────────────┘
                                        │
                                        ▼
                         ┌─────────────────────────────┐
                         │ Streamlit UI: app.py        │
                         │ upload, chat, overview, PBI │
                         └───────┬─────────────┬───────┘
                                 │             │
                       question │             │ table/data actions
                                 ▼             ▼
                    ┌──────────────────┐  ┌─────────────────────┐
                    │ Agent: agent.py  │  │ UI helpers:         │
                    │ LLM tool loop    │  │ webapp_logic.py     │
                    └────────┬─────────┘  └──────────┬──────────┘
                             │ tool calls             │
                             └──────────┬─────────────┘
                                        ▼
                    ┌─────────────────────────────────────┐
                    │ Python tools                        │
                    │ tools.py       profiling, SQL,      │
                    │                cleaning, charts     │
                    │ powerbi.py     DAX, view, insights  │
                    └──────────────────┬──────────────────┘
                                       │ SQLAlchemy
                    ┌──────────────────▼──────────────────┐
                    │ MySQL database (or DATABASE_URL DB) │
                    │ source, upload, cleaned tables,     │
                    │ optional view and saved insights    │
                    └─────────────────────────────────────┘

                    agent.py ── OpenAI-compatible chat API ──► LLM provider
                    tools.py ── Plotly ──► HTML chart files / Streamlit chart
                    powerbi.py ──► DAX and plan files for Power BI
```

### Main components

| Component | Responsibility |
|---|---|
| [`app.py`](./app.py) | Streamlit entry point. Provides source selection, file upload, permissions, chat, data overview, chart rendering, and the Power BI tab. |
| [`src/agent.py`](./src/agent.py) | Builds the OpenAI-compatible LLM client, supplies the schema and tool definitions, runs the tool-call loop, manages short conversational history, and returns the final answer and tool trace. |
| [`src/tools.py`](./src/tools.py) | Database engine and table helpers; schema profiling; SQL validation/execution; generic cleaning; chart suggestions and Plotly chart generation. |
| [`src/webapp_logic.py`](./src/webapp_logic.py) | UI-independent upload parsing, column/table naming, table listing, starter questions, chart-spec handling, and insight SQL extraction. |
| [`src/powerbi.py`](./src/powerbi.py) | Builds dataset-specific DAX and layout plans, creates a Title Case-column database view, and saves optional findings. |
| [`src/clean_data.py`](./src/clean_data.py) | One-time, Blinkit-example preparation pipeline: standardizes the example columns, fixes selected known data issues, writes a CSV, and loads the result into MySQL. |
| [`tests/`](./tests/) | Tests for the agent, tools, Power BI functions, and web-app logic. Most database tests use temporary SQLite databases and fake LLM responses. |

### How a question is answered

1. The selected table name is set as the default table for the tools.
2. The agent profiles the selected table and builds a compact schema hint (column names, inferred roles, ranges/top values, and known issues). The hint is cached for the current Python process.
3. `agent.py` sends the question, recent conversation, schema, and function/tool schemas to the configured LLM through the OpenAI-compatible chat-completions API.
4. The LLM may request a tool call. The agent dispatches it to a Python function registered in `tools.py` or `powerbi.py`.
5. For a numerical question, `run_sql` validates and executes the generated read query against the database. Its results are returned to the LLM, which writes the answer.
6. For a chart, `make_chart` checks the requested columns against the table schema, builds an aggregate query (or reads a limited sample for a scatter plot), and renders the result with Plotly.
7. The UI displays the answer, chart(s), and an expandable list of tool calls. The agent allows up to eight tool-call rounds for one question and keeps up to six prior user turns for follow-ups.

If a question asks for a time trend but the data has no suitable date/time column, the intended behavior is to say that the data cannot answer it. For the included example, `outlet_establishment_year` describes outlet age/year; it is not a date for each sale.

### Available agent tools

| Tool | Purpose | Database write? |
|---|---|---|
| `profile_data` | Summarizes columns, inferred roles, nulls, numeric statistics, common categorical values, duplicate rows, and likely quality issues. | No |
| `run_sql` | Executes a single query accepted by the read-query validator and returns at most 200 rows. | Intended for reads only |
| `suggest_visuals` | Uses column types and values to suggest charts for the dataset. | No |
| `make_chart` | Queries data for one of the supported chart types and writes an HTML chart under `charts/`. | Writes a local file |
| `clean_data` | Applies generic cleaning and writes the result to a new table (default `<table>_clean`). | Yes; requires approval in the chat flow |
| `powerbi_plan` | Generates DAX measures and a dashboard layout plan under `powerbi/`. | Writes local files |
| `create_powerbi_view` | Recreates a view with Power BI-friendly column names. | Yes; requires approval in the chat flow |
| `save_insight` | Appends a question, answer, and optional SQL to `agent_insights`. | Yes; initiated explicitly by the user |

The UI's **Allow the agent to change the database** control is required for the in-chat cleaning and view-creation operations. Uploading a file and using **Save as insight** are separate, user-initiated database writes.

## Data sources and data flow

### Existing MySQL tables

The sidebar lists tables visible to the configured database account. `DB_TABLE` is the default table name in the tools (defaults to `blinkit_sales`); in the app, the selected table becomes the active table for that session. The `agent_insights` table is hidden from the normal table selector.

### CSV and Excel uploads

`src/webapp_logic.py` reads the uploaded file, drops completely empty rows/columns, converts column names to lowercase SQL-friendly `snake_case`, resolves duplicate names, and stores the result in a table named `upload_<filename>`. Uploads are limited to 200,000 rows. Uploading another file with the same derived table name replaces that table.

### Included Blinkit example

The current example data is [`data/clean_data.csv`](./data/clean_data.csv): 8,523 rows and 14 columns, including product attributes, outlet attributes, sales, rating, outlet age, and item category.

`src/clean_data.py` looks first for `data/BlinkIT_Grocery_Data.xlsx`; if it is absent, it uses the included `data/clean_data.csv`. It then applies the example-specific preparation steps, saves `data/clean_data.csv`, and replaces the `blinkit_sales` table in the configured MySQL database. The script does **not** create the MySQL database itself.

The separate interactive cleaner in `tools.py` is generic: it trims text, can apply an explicit value map, merges case/spacing variants, removes exact duplicate rows, fills numeric nulls with medians and text nulls with `"Unknown"`, and writes to a new table. Review the result before using it; generic cleaning cannot determine the correct business meaning of every value.

## Example questions

- Which outlet type has the largest total sales?
- What share of sales comes from each item type?
- How does average rating vary by outlet type?
- Which categories should I compare in a chart?
- What data-quality issues are present?
- Prepare DAX measures and a Power BI dashboard layout for this table.

The test suite checks the included example's known top groups: **Supermarket Type1** for outlet-type sales and **Fruits and Vegetables** for item-type sales. These are findings from the sample file only, not statements about all Blinkit sales.

## Requirements

- Python 3.10 or later (the project code uses modern Python syntax; use a currently supported Python version).
- MySQL server and an existing database for the MySQL workflow.
- Credentials for an OpenAI-compatible LLM provider with chat-completions tool/function calling enabled.
- Power BI Desktop is optional and only needed to use the generated Power BI output.

Python packages used by the app and tests: `streamlit`, `pandas`, `numpy`, `plotly`, `SQLAlchemy`, `PyMySQL`, `python-dotenv`, `openai`, `openpyxl`, `xlrd` (for legacy `.xls` files), and `pytest`.

## Setup

Run commands from the `blinkit-agent` project directory.

### 1. Create and activate a virtual environment

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install streamlit pandas numpy plotly sqlalchemy pymysql python-dotenv openai openpyxl xlrd pytest
```

If PowerShell prevents environment activation, run the interpreter directly as `.venv\Scripts\python.exe -m ...`, or use the equivalent activation command for your shell.

### 2. Configure the database

Create a database in MySQL if you do not already have one. For example, from a MySQL client:

```sql
CREATE DATABASE blinkit_agent;
```

Create a local `.env` file in the project directory. Do not commit this file or put real credentials in source control.

```dotenv
DB_HOST=localhost
DB_NAME=blinkit_agent
DB_USER=your_mysql_user
DB_PASSWORD=your_mysql_password
DB_TABLE=blinkit_sales
```

The app's standard connection uses `mysql+pymysql`. `DB_NAME` must refer to an already-created database. For better separation of privileges, optionally configure a read-only database account for analysis:

```dotenv
DB_RO_USER=your_read_only_user
DB_RO_PASSWORD=your_read_only_password
```

Read-only operations use this account when configured. Uploading, cleaning, creating the Power BI view, and saving insights require a database account with the relevant write permissions.

For local development or tests, `DATABASE_URL` can override the MySQL settings, for example:

```dotenv
DATABASE_URL=sqlite:///blinkit_agent.db
```

With SQLite, the app can create tables from file uploads without a MySQL server. The database file is relative to the working directory. To analyze the included example without a MySQL server, you can import `data/clean_data.csv` using the app's upload workflow.

### 3. Configure an LLM provider

The agent uses the `openai` Python SDK with an OpenAI-compatible chat API. Choose one configuration.

**Google AI Studio / Gemini or another compatible endpoint:**

```dotenv
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_MODEL=your_tool-capable_model_name
```

**Groq:**

```dotenv
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=your_tool-capable_model_name
```

The implementation provides Groq's OpenAI-compatible endpoint automatically when `GROQ_API_KEY` is set. `LLM_*` values take precedence over corresponding `GROQ_*` values. Use a provider/model that supports tool calling; exact model names and availability depend on the provider.

Optional agent settings:

| Variable | Default | Description |
|---|---:|---|
| `LLM_TIMEOUT` | `60` seconds | Timeout for an LLM request. |
| `LLM_REASONING_EFFORT` | Provider default; Gemini defaults to low | Optional reasoning-effort hint. The agent retries without it if a provider rejects the parameter. |
| `LLM_MAX_TOOL_CHARS` | `6000` | Maximum characters of tool output passed back to the LLM before truncation. |
| `PBI_TABLE` | `pbi_<table>` | Default Power BI table name used in generated DAX. |

### 4. Prepare the example table (MySQL workflow)

If you want the example preloaded into MySQL, verify that `data/clean_data.csv` exists (it is included) or add the source workbook at `data/BlinkIT_Grocery_Data.xlsx`, then run:

```powershell
python src\clean_data.py
```

This writes the prepared CSV and replaces the `blinkit_sales` table. If you prefer to use another dataset, skip this step and upload it in the app or select an existing MySQL table.

## Run the application

From the project directory:

```powershell
streamlit run app.py
```

Open the local URL printed by Streamlit. In the sidebar, select a table or upload a file. Then use:

- **Ask the agent** to ask questions and review answers, charts, and tool traces.
- **Data overview** to inspect the data and optionally create a cleaned copy.
- **Power BI** to generate measures/layout guidance or create/refresh a database view.

To check the LLM configuration with a small API request (does not access the database):

```powershell
python src\agent.py --ping
```

To run the command-line chat instead of the web app:

```powershell
python src\agent.py
```

## Power BI workflow

1. In the app, open **Power BI** and select **Generate Power BI plan**. The project infers a numeric measure and candidate categories from the selected table.
2. Review and download the generated DAX and plan files. Add DAX measures one at a time in Power BI Desktop under **Modeling > New measure**.
3. If you need a consistently named source table, enable database changes and create/refresh the `pbi_<table>` view.
4. In Power BI Desktop, connect to the configured MySQL server/database and load the view (the app also shows SQL and Power Query M instructions).
5. Build the report visuals using the generated layout. Refresh the Power BI data after source changes.

Plan generation writes files under `powerbi/`. View creation is a separate database operation and requires write permission. Saved findings are appended to `agent_insights`; the generated plan describes how to load that table for an optional insights page.

## Project files and generated outputs

```text
blinkit-agent/
├── app.py                         # Streamlit UI
├── data/
│   └── clean_data.csv             # Included prepared example dataset
├── src/
│   ├── agent.py                   # LLM client and tool-calling loop
│   ├── clean_data.py              # Blinkit example ETL/import script
│   ├── powerbi.py                 # DAX plans, database view, saved insights
│   ├── tools.py                   # Data/SQL/chart tools
│   └── webapp_logic.py            # Upload and UI helper functions
├── tests/                         # Automated tests
├── charts/                        # Generated interactive HTML charts
├── powerbi/                       # Generated DAX and dashboard plan files
├── .env                           # Local secrets/configuration; do not commit
└── blinkit_dashboard_mysql.pbix   # Included Power BI report artifact
```

Generated chart files are saved under `charts/`; DAX and Markdown plans are saved under `powerbi/`. Some example outputs and a `.pbix` file are already present in the project.

## Tests

Run the test suite from the project directory:

```powershell
python -m pytest -q
```

The tests use temporary SQLite databases and fake LLM responses where possible, so they do not need a live MySQL server or LLM API key. The data/tool tests use `data/clean_data.csv`; if that file is missing, those tests are skipped. The suite covers SQL validation, expected sample-data results, chart generation, cleaning, tool-call behavior and approvals, Power BI output, and web-app helper logic.

## Safety, privacy, and limitations

- Analysis is designed around read queries. `run_sql` accepts a single `SELECT`/`WITH` query, rejects common write/administrative keywords, and limits rows returned to the LLM. This is application-level validation, **not** a substitute for database permissions or a security boundary. Use a least-privilege read-only database account for analysis.
- Cleaning and Power BI view creation modify the database and require user approval in the agent flow. Cleaning writes a new table; view creation recreates the named view. Uploading a same-named file replaces its upload table.
- The LLM provider receives the question, schema context, conversation history, and tool results needed to answer. Do not send sensitive data to a provider unless your organization permits it.
- The answer quality depends on the selected table, column names/types, data quality, and the model's interpretation. Verify important findings against the underlying data.
- The app describes observed data; it does not establish causes or account for confounding factors on its own.
- The sample is static and is not a live Blinkit feed. Time-based analysis requires suitable date/time data in the selected dataset.
- The upload limit is 200,000 rows. SQL results returned to the model are limited to 200 rows, and the agent has a finite tool-call budget per question.
- Power BI plans are suggestions generated from inferred column roles; users should check the measures, table names, and visuals in Power BI.

## Troubleshooting

| Symptom | Checks |
|---|---|
| Cannot connect to the database / no tables appear | Confirm the MySQL server is running, the database exists, and `DB_HOST`, `DB_NAME`, and credentials in `.env` are correct. |
| Upload or write operation fails | Confirm the account used for writes has permission to create/replace tables. If a read-only account is configured, write operations use `DB_USER` / `DB_PASSWORD` instead. |
| Agent reports missing LLM configuration | Set either `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`, or `GROQ_API_KEY` and `GROQ_MODEL`. |
| Provider rejects tool calls or a model name | Choose a currently available model that supports function/tool calling; confirm its name and endpoint with the provider. |
| No charts or measures are suggested | Check that the selected table has a usable numeric column and meaningful category/time columns. |
| Power BI cannot navigate to the view | Use the SQL statement shown in the app's MySQL connector instructions; some Power BI MySQL connector versions do not expose views in navigation. |

