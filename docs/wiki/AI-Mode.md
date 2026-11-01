# AI Mode (customers)

AI Mode answers plain-language questions using only the search
index — every claim links back to a source with a `[n]` chip.

## How it works

1. Switch to the **AI chat** tab (next to Search).
2. Ask anything, e.g. "summarize today's top news headlines".
3. The agent rewrites your question, searches the index through a
   small budgeted tool set, reads the top sources, and synthesizes
   an answer with citations.

The transcript persists in your browser across visits.

## Honest limits

- **No model, no prose**: with no AI backend enabled (or on model
  error) you get standard results plus a notice — never a 500, never
  made-up text. Look for the `Powered by …` line naming the model.
- **Fallback answers** are extractive: stitched lead sentences from
  the sources with matching `[n]` markers, labelled in the agent
  trace (`summary_fallback`).
- **Ungrounded prose is replaced**: an answer with no citation
  markers is swapped for verbatim source text.
- The **Agent trace** section shows the rewrite, tool steps,
  fallbacks, and model-call count for every turn.
- **Sources** disclosure lists the hits behind the answer; expanding
  it shows the same [Perspectives](Perspectives) block as search.
