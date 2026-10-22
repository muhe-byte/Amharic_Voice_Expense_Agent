# Amharic Voice Expense Agent

**Status:** Initial design / implementation in progress\
**Purpose:** Turn Amharic voice expense notes into validated,
user-approved records and answer questions about recorded spending.\
**Initial stack:** Telegram Bot API · Python · Google Gemini · Google
Sheets\
**Timezone:** `Africa/Addis_Ababa` · **Currency:** ETB

> This README documents the intended design and build plan. Do not
> describe a feature as implemented until it has been coded and tested.
> Version 1 focuses on expense recording and spending questions. Bill
> reminders, English support, and Ethiopian-calendar dates are future
> work unless explicitly implemented.

## 1. Problem and goal

Everyday expenses are easy to forget. This project lets a user send a
voice message to a Telegram bot, such as:

> ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።

Illustrative meaning: "I spent 250 birr on transport yesterday."

The application should transcribe the message, interpret the user's
intent, extract structured fields, validate them, show a confirmation
summary, save only after approval, and report success only after Google
Sheets confirms the write.

It should also answer questions such as "How much did I spend on
transport this week?" by retrieving matching records and calculating the
total in Python---not by creating a new expense.

## 2. What makes it agentic?

An LLM alone generates outputs. An agentic application uses a model
within a controlled system that can select tools, observe tool results,
and decide whether to continue, ask a question, request approval, or
finish.

The intended loop is:

**Observe → Reason → Validate → Confirm (when needed) → Act → Observe
result → Finish or continue**

-   **Observe:** receive a Telegram message and relevant conversation
    state.
-   **Reason:** Gemini interprets intent and proposes structured fields
    or an allowed tool call.
-   **Validate:** Python checks the proposal against schemas and
    business rules.
-   **Confirm:** the user approves the exact pending expense before a
    write.
-   **Act:** Python executes an explicitly permitted tool.
-   **Observe:** inspect the actual API result.
-   **Finish or continue:** return verified success, ask a follow-up, or
    report failure.

The model cannot execute arbitrary Python or access arbitrary files.
Python defines the tools, checks their arguments, enforces permissions,
and limits the number of steps. A tool call alone does not automatically
make a system agentic.

## 3. Architecture

``` mermaid
flowchart TD
    U[User] --> T[Telegram Bot API]
    T --> P[Python orchestrator]
    P --> A{Text or voice?}
    A -->|Voice| D[Download audio]
    D --> G[Gemini audio transcription or configured STT]
    G --> TR[Transcript]
    A -->|Text| TR
    TR --> I[Gemini intent and field extraction]
    I --> V[Python schema and business-rule validation]
    V --> C{Clear and valid?}
    C -->|No| Q[Ask clarification]
    Q --> T
    C -->|Yes| R{Intent}
    R -->|Add expense| CF[Show proposal and request confirmation]
    CF -->|Approved| S[Google Sheets write tool]
    CF -->|Cancelled| X[Cancel pending action]
    R -->|Spending question| GET[Read-only Sheets query]
    GET --> CALC[Calculate in Python]
    S --> CHECK[Inspect tool result]
    CALC --> CHECK
    CHECK --> RESP[Compose truthful response]
    X --> RESP
    RESP --> T
```

### Component responsibilities

  -----------------------------------------------------------------------
  Component                           Responsibility
  ----------------------------------- -----------------------------------
  Telegram                            Receives messages and sends
                                      replies.

  Python orchestrator                 Controls flow, state, permissions,
                                      validation, tools, and errors.

  Gemini                              Transcribes supported audio (if
                                      supported by the selected model),
                                      interprets intent, extracts fields,
                                      and may select an allowed tool.

  Validation layer                    Checks required fields, types,
                                      amount, category, date, and
                                      consistency.

  Confirmation handler                Links approval to the exact,
                                      unexpired pending action.

  Google Sheets API                   Stores and retrieves records.

  Python calculations                 Performs deterministic totals and
                                      arithmetic.
  -----------------------------------------------------------------------

**Audio implementation note:** Verify the currently supported Gemini
model, SDK, and audio formats before coding. If the chosen model cannot
handle the required audio input, use a separate speech-to-text service.
Amharic transcription quality must be tested; it must not be assumed.

## 4. End-to-end workflow: record an expense

1.  **Receive:** Telegram delivers an update. Capture chat ID, message
    ID/update ID, message type, and receipt time.

2.  **Download:** for voice messages, retrieve the file and validate its
    size and format. Convert it only if the selected API requires it.

3.  **Transcribe:** Gemini or the configured speech-to-text component
    returns a transcript. Preserve the original language and meaning; do
    not silently translate or summarize.

4.  **Expose the transcript:** let the user inspect what was heard
    before saving. If unclear, ask the user to retry or clarify.

5.  **Extract:** Gemini proposes structured fields, for example:

    ``` json
    {
      "intent": "add_expense",
      "amount": 250,
      "currency": "ETB",
      "category": "Transport",
      "date_expression": "yesterday",
      "description": "Transport expense"
    }
    ```

    This is illustrative, not a real model result.

6.  **Validate in Python:** check required fields, types, positive
    amount, supported currency, category, date, transcript consistency
    where reliable, and duplicate status. Do not assume the LLM
    correctly parses every Amharic number expression.

7.  **Resolve date:** resolve "today" and "yesterday" in Python using
    the message timestamp and `Africa/Addis_Ababa`. Store the resolved
    date as Gregorian ISO format (`YYYY-MM-DD`). Ethiopian-calendar
    support is future work.

8.  **Request approval:** show the transcript and parsed record.
    Example:

    ``` text
    Amount: 250 ETB
    Category: Transport
    Date: [actual date resolved by Python]
    Description: Transport expense

    Save this expense?
    [YES] [NO] [Cancel]
    ```

9.  **Handle response:** confirm only the current pending action;
    editing triggers validation and confirmation again; cancellation
    discards it; unclear responses prompt clarification; expired
    approvals cannot be reused.

10. **Write:** call the Google Sheets tool only after approval and final
    validation.

11. **Check result:** inspect the API response. Tell the user it was
    saved only after a successful write. Handle failures without
    creating duplicate rows.

## 5. Workflow: answer a spending question

Example: "How much did I spend on transport this week?"

1.  Receive text or transcribed voice.
2.  Gemini classifies the intent as a spending query.
3.  Extract and validate category and date range.
4.  Query matching rows using a read-only tool.
5.  Calculate the total in Python.
6.  Return the total, currency, and period.
7.  Do not create or modify a transaction.

Define what "this week" means in application configuration (including
the week-start day). Ask a question if the date range is ambiguous.

## 6. Workflow: missing information or failure

**Missing amount:** "I spent money yesterday." The application asks "How
much did you spend?" and saves nothing until the record is complete,
validated, and confirmed.

**Unclear transcript:** show it and ask the user to retry or clarify.
Never guess the amount.

**Gemini returns malformed data:** reject the output; use a bounded
retry or ask the user. No write.

**Google Sheets fails:** report failure honestly. Any retry must be
bounded and designed to avoid duplicate writes.

**Duplicate Telegram delivery:** use a stable idempotency key (for
example, `chat_id:message_id` or the update ID) so the same update does
not create duplicate records.

## 7. Tools and permissions

The LLM may request only explicitly defined tools; Python executes them.

  -------------------------------------------------------------------------
  Tool                      Purpose                 Access
  ------------------------- ----------------------- -----------------------
  `add_expense`             Save a validated and    Write
                            confirmed expense.      

  `get_expenses`            Retrieve records        Read-only
                            matching approved       
                            filters.                

  `calculate_spending`      Calculate totals from   Read-only computation
                            retrieved records in    
                            Python.                 

  `ask_for_clarification`   Ask for missing or      Conversation action
                            ambiguous details.      
  -------------------------------------------------------------------------

`ask_for_clarification` can be a controlled orchestrator response rather
than a database function. Start with a small tool set. Enforce an
allowlist, typed arguments, maximum agent steps, and clear stop states.

## 8. Data model

Suggested Google Sheets columns:

  Column                Purpose
  --------------------- --------------------------------------------------
  `transaction_id`      Application-generated unique ID
  `created_at`          Record creation timestamp
  `transaction_date`    Resolved date (`YYYY-MM-DD`)
  `amount`              Numeric amount
  `currency`            Initially `ETB`
  `category`            Normalized category, e.g. Transport or Food
  `description`         Short expense description
  `transcript`          Optional transcript, if retention is needed
  `chat_id`             Telegram chat identifier; treat as personal data
  `message_id`          Telegram message ID within the chat
  `source_message_id`   Composite idempotency key

Illustrative fictional row:

  ----------------------------------------------------------------------------------------
  transaction_id   transaction_date           amount currency    category    description
  ---------------- ------------------ -------------- ----------- ----------- -------------
  TX-0001          2026-10-20                    250 ETB         Transport   Transport
                                                                             expense

  ----------------------------------------------------------------------------------------

Avoid storing transcripts if they are not needed. Google Sheets is
convenient for a student prototype, but it has limitations around
concurrency, transactions, indexing, and multi-user production
workloads.

## 9. Validation and safety requirements

-   Never save an expense without a valid amount and resolvable date.
-   Reject invalid structured output even if Gemini returns it
    confidently.
-   Cross-check the extracted amount against the transcript where a
    reliable deterministic parser exists. If not, ask the user to
    confirm.
-   Show the user the proposed record before saving.
-   Bind confirmation to the current pending action; expire stale
    confirmations.
-   Revalidate any edited proposal and ask for approval again.
-   Cancelled or expired proposals cannot be written.
-   Read-only queries must not call write tools.
-   Prevent duplicate writes on retried messages.
-   Report success only after the storage service confirms success.
-   Never execute arbitrary code generated by the LLM.
-   Bound retries and agent steps.
-   Do not log secrets or unnecessary financial details.

**Validation and confirmation are different:** validation checks whether
the data is acceptable; confirmation checks whether the user agrees with
the proposed record.

## 10. Proposed repository structure

``` text
personal-finance-agent/
├── README.md
├── main.py
├── agent.py
├── tools.py
├── speech_to_text.py
├── sheets_service.py
├── validation.py
├── config.py
├── tests/
│   ├── test_validation.py
│   ├── test_dates.py
│   ├── test_intent_routing.py
│   ├── test_duplicate_prevention.py
│   └── test_sheets_service.py
├── sample_data/
│   └── sample_transactions.csv
├── .env.example
├── .gitignore
└── requirements.txt
```

Suggested responsibilities: - `main.py`: entry point and Telegram
handlers. - `agent.py`: Gemini requests, intent handling, tool-selection
loop, stop conditions. - `tools.py`: allowed tool definitions and
dispatch. - `speech_to_text.py`: audio processing and transcription
integration. - `sheets_service.py`: Sheets authentication, reads,
writes, and API errors. - `validation.py`: schemas, amount checks,
categories, and dates. - `config.py`: configuration loading and
defaults. - `tests/`: automated tests.

This is a proposed structure. Simplify it if needed; do not create empty
modules without a clear purpose.

## 11. Setup overview

Exact commands and dependencies should be confirmed once the
implementation is selected.

``` bash
git clone <your-repository-url>
cd personal-finance-agent
python -m venv .venv
```

Activate the environment:

**Windows PowerShell**

``` powershell
.venv\Scripts\Activate.ps1
```

**macOS / Linux**

``` bash
source .venv/bin/activate
```

Once `requirements.txt` exists:

``` bash
pip install -r requirements.txt
```

Then: 1. Create a Telegram bot and obtain its token. 2. Create a Gemini
API key and verify the model/API. 3. Create a Google Cloud project and
enable the Google Sheets API. 4. Configure the selected Google
credentials method. 5. Create a spreadsheet and identify its ID and
worksheet. 6. Grant the configured identity only the access it needs. 7.
Copy `.env.example` to `.env` and fill in local values. 8. Run unit
tests before connecting external services. 9. Start the bot using the
command documented by the implementation.

Do not commit credentials or `.env`.

## 12. Configuration template

A future `.env.example` may contain:

``` dotenv
TELEGRAM_BOT_TOKEN=
GEMINI_API_KEY=
GEMINI_MODEL=
GOOGLE_SHEETS_SPREADSHEET_ID=
GOOGLE_SHEETS_WORKSHEET=
GOOGLE_APPLICATION_CREDENTIALS=
APP_TIMEZONE=Africa/Addis_Ababa
DEFAULT_CURRENCY=ETB
MAX_AGENT_STEPS=5
PENDING_CONFIRMATION_TTL_SECONDS=300
```

These names are proposed; the code must implement the settings it uses.
Keep real credentials out of source control. `.gitignore` should exclude
`.env`, credential files, virtual environments, caches, local logs, and
private data.

## 13. Build milestones

### Milestone 1 --- Text expense pipeline

-   [ ] Accept a text expense request.
-   [ ] Extract intent and structured fields with Gemini.
-   [ ] Validate output and resolve dates in Python.
-   [ ] Display a confirmation summary.
-   [ ] Save only after confirmation.
-   [ ] Verify the Google Sheets response.
-   [ ] Test using fictional data.

### Milestone 2 --- Telegram text interaction

-   [ ] Receive and route Telegram text.
-   [ ] Implement confirmation, edit, cancellation, and expiry.
-   [ ] Handle Telegram errors safely.

### Milestone 3 --- Amharic voice

-   [ ] Download a Telegram voice message.
-   [ ] Validate audio size and format.
-   [ ] Transcribe using supported Gemini audio functionality or a
    separate speech-to-text service.
-   [ ] Display transcript before saving.
-   [ ] Test number words, digits, mixed-language speech, and noisy
    recordings.
-   [ ] Ask for clarification when uncertain.

### Milestone 4 --- Spending questions

-   [ ] Route queries to read-only tools.
-   [ ] Retrieve matching rows.
-   [ ] Calculate totals in Python.
-   [ ] Define date-range behavior.
-   [ ] Verify queries do not write records.

### Milestone 5 --- Reliability and documentation

-   [ ] Prevent duplicate writes.
-   [ ] Test API failures and retries.
-   [ ] Add unit and integration tests.
-   [ ] Add real demo screenshots and setup instructions.
-   [ ] Document known limitations and measured results.

### Future milestone --- Bill reminders

-   [ ] Extract bill name, amount, due date, and reminder time.
-   [ ] Validate and confirm the reminder.
-   [ ] Store reminder data.
-   [ ] Implement a separate scheduler.
-   [ ] Send Telegram notifications.
-   [ ] Prevent duplicate notifications and handle missed scheduler
    runs.

Saving a reminder is not the same as scheduling and delivering a
notification later.

## 14. Testing and evaluation

Test with a small documented set. Do not invent accuracy metrics.

  -----------------------------------------------------------------------
  Test case                           Expected behavior
  ----------------------------------- -----------------------------------
  Clear Amharic expense               Transcript and proposal shown;
                                      confirmation required

  Amount in digits                    Extract and validate amount

  Amharic number words                Correctly parse supported forms,
                                      otherwise ask

  Mixed words and digits              Test explicitly; don't assume
                                      success

  Missing amount                      Ask; save nothing

  Ambiguous date                      Ask or apply a documented rule

  Spending question                   Read-only query and Python
                                      calculation

  User cancels                        No write

  User edits amount                   Revalidate and reconfirm

  Duplicate update                    No duplicate record

  Malformed model output              Reject; no write

  Sheets API failure                  Report failure honestly

  Expired confirmation                Reject stale approval

  Unclear audio                       Ask for retry or clarification
  -----------------------------------------------------------------------

Measure after actual experiments: - transcription quality on test
recordings; - intent classification accuracy; - amount extraction
accuracy; - date resolution accuracy; - tool-selection accuracy; -
spending-calculation correctness; - duplicate-prevention success; -
write success rate under test conditions.

For Amharic, use self-recorded or appropriately consented audio with
manually checked reference transcripts. Report the sample size and
limitations. Do not claim reliability based on one successful demo.

## 15. Security and privacy

-   Use fictional or personal test data carefully.
-   Never commit API keys, Telegram tokens, service-account files, or
    `.env`.
-   Restrict spreadsheet access to the minimum necessary identities.
-   Avoid retaining transcripts unless required.
-   Avoid logging complete financial messages by default.
-   Protect the bot against unauthorized access and unexpected tool
    inputs.
-   Validate all model-generated tool arguments.
-   Require confirmation before financial writes.
-   Use bounded, duplicate-safe retries.
-   Document what is stored and how records can be deleted.
-   Do not claim production-grade security without review.

## 16. Current limitations and future work

Known limitations: - Amharic transcription and number extraction must be
measured. - Dates initially use Gregorian dates and the configured
timezone. - Google Sheets is a prototype data store, not necessarily the
right production database. - Public deployment requires stronger
authorization, privacy controls, persistence, and monitoring. - Bill
reminders require a scheduler and delivery logic.

Possible future work: - Bill and recurring-payment reminders. - English
and mixed-language input. - Improved Amharic number parsing. -
Ethiopian-calendar support. - Stronger multi-user storage and
authorization. - Optional budgets and spending summaries. - A dashboard
if user testing shows it is useful.

## 17. Demo checklist

-   [ ] Credentials are configured outside source control.
-   [ ] The intended spreadsheet is accessible.
-   [ ] A clear expense works end to end.
-   [ ] Transcript and extracted fields are visible.
-   [ ] Confirmation is required before saving.
-   [ ] The new row appears in Google Sheets.
-   [ ] A spending query returns a calculated result.
-   [ ] A missing-amount case is handled safely.
-   [ ] Cancellation does not write a row.
-   [ ] A failed write does not produce a success message.
-   [ ] Demo data is fictional or appropriate.
-   [ ] A backup screenshot or recording is available.

## 18. References

Use current official documentation when implementing; model availability
and API behavior can change.

-   Telegram Bot API: https://core.telegram.org/bots/api
-   Gemini API documentation: https://ai.google.dev/gemini-api/docs
-   Gemini function calling:
    https://ai.google.dev/gemini-api/docs/function-calling
-   Google Sheets API --- append values:
    https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values/append
-   Google Sheets API --- Python quickstart:
    https://developers.google.com/sheets/api/quickstart/python
-   Python documentation: https://docs.python.org/3/

------------------------------------------------------------------------

## Project principle

**Gemini proposes; Python validates and controls; tools perform actions;
Google Sheets stores records; the user approves financial writes.**

The goal is not maximum autonomy. The goal is a useful, testable,
understandable, and trustworthy agent.
