MIT License

Copyright (c) 2026 @rajanbajaj

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## Meta models and Meta AI Developer Hackathon — additional terms

This section does not replace the MIT license above for the project's
original code. It records the extra obligations that apply when Meta
models or hosted Meta APIs are used with this project, and when this
project is submitted to the Meta Global AI Developer Hackathon
(Oct 26 – Nov 4, 2026, hosted by Meta and Cerebral Valley). It is a
compliance summary, not legal advice; the linked license texts and the
hackathon's Official Rules govern in case of any conflict.

### 1. Current status: no Meta models in use

As shipped, this project bundles and calls no Meta models or Meta APIs.
`configs/models.yaml` defines only `fake` (offline test stand-in),
`claude`, `muse`, and `local` backends, all disabled by default, and no
model weights, API keys, or Meta SDKs are vendored in this repository.
Accordingly, no Meta model license currently attaches to this code. The
moment a Meta model or hosted Meta API is integrated or called, the
terms below apply to that use.

### 2. Using Meta open-weights models (Llama family)

Downloading, self-hosting, or building on Meta open-weights models
(including via partners such as Baseten, Modal, or Fireworks) is
governed by the applicable Llama Community License Agreement, not by
the MIT license above. Anyone who adds such use to this project must
comply with, at minimum:

- **Attribution.** Prominently display "Built with Llama" on a related
  website, user interface, blog post, about page, or product
  documentation, and retain the license's attribution Notice text file
  in all distributed copies of the Llama Materials.
- **Acceptable Use.** All use must comply with applicable laws and the
  Llama Acceptable Use Policy, which the license incorporates by
  reference.
- **Scale threshold.** If the products or services using the models
  exceed 700 million monthly active users, a separate license must be
  requested from Meta; the community license alone no longer covers
  that use.
- **Derivative models.** An AI model created, trained, fine-tuned, or
  otherwise improved using the Llama Materials or their outputs must
  begin its name with "Llama" when distributed or made available.
- **No trademark grant.** No Meta trademark license is granted beyond
  what the attribution clause requires; goodwill in the "Llama" mark
  belongs to Meta.
- **Termination.** Breach terminates the license (requiring deletion
  and ceased use), and patent/IP litigation against Meta over the
  Llama Materials terminates the grant as of filing.

Reference: Llama Community License Agreement
(https://github.com/meta-llama/llama-models — see the LICENSE file of
the model version used) and the Llama Acceptable Use Policy
(https://www.llama.com/llama3_3/use-policy, or the policy page of the
version used).

### 3. Using Meta hosted APIs (Meta Model API credits and keys)

Hackathon participants receive Meta Model API credits for use during
the event. Hosted calls (e.g. Muse Spark / Wonder, Muse Image, SAM via
`https://api.meta.ai`) are governed by Meta's API terms in addition to
this project's license: use only valid issued credentials, stay within
the granted credit/rate limits, and do not use model outputs to train
or improve other models except as the applicable terms permit. API keys
belong in environment variables (e.g. `MUSE_API_KEY`-style entries in
`configs/models.yaml` resolve from the environment) and must never be
committed — this repository's secret-scanning and `.gitignore` posture
exists to enforce exactly that.

### 4. Hackathon submission terms

- **Original work.** Submissions must be original work created during
  the hackathon period. Third-party code, models, and data (including
  any Meta materials) may be used only with a valid license or
  permission, and their license terms must be honored in the
  submission.
- **Ownership.** Participants retain ownership of the intellectual
  property they create, subject to the Official Rules; submitting means
  representing ownership or permission for all submitted materials.
- **Meta-tool use is judged.** Creative use of Meta's AI tools carries
  significant scoring weight, which is why sections 2–3 above matter
  for any submission that integrates them.
- **Governing document.** The event's Official Rules (linked from
  https://cerebralvalley.ai/e/meta-ai-developer-hackathon) control
  eligibility, deadlines, judging, and prizes.

If any clause above conflicts with the MIT license for the project's
original code, each governs its own subject matter: MIT for the code
written here, Meta's licenses and the Official Rules for Meta
materials, APIs, and the submission itself.
