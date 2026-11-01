# Feedback (customers)

Every result card offers **Report incorrect** and **Report
suspicious**; the search experience also asks whether a result was
useful. All feedback is anonymous by construction:

- Stored as `(doc id, action, reason)` counts only — never query
  text, sessions, or identifiers.
- A closed reason taxonomy ("what was not right": privacy,
  kids-safety, …) is accepted only with negative/report actions.
- Aggregates surface in the admin dashboard to steer ranking and
  safety work; raw tallies never leave the backend as user data.

Abuse note: feedback nudges future ranking; it never edits the index
or removes a page by itself.
