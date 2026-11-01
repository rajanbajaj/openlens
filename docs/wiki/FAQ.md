# FAQ (customers)

**Does OpenLens use Google/Bing under the hood?**
No. The only searchable data is what its own crawler indexed into
Postgres. A small index has gaps; [Feedback](Feedback) and recrawls
close them.

**Why do some searches return little or nothing?**
Category modes (news, shopping, travel, child-safe) only show pages
the crawl classified there. Try default mode, or suggest a source
via feedback.

**Is my search history stored?**
No. Recent searches live in your browser's local storage; the
backend logs one JSON line per request with latencies and error
counts — never the query text.

**What does "Powered by …" mean?**
The AI backend that worded an answer or enrichment. Absent means
rule-based or extractive output.

**Why was content blocked?**
Child-safe mode and category blocks filter before ranking and say
so in the trace/diagnostics. Challenge a wrong block with
**Report incorrect** on the card.

**Does it cost anything?**
Self-hosted: your own servers and any model API usage. No accounts,
no per-search fees in the product.
