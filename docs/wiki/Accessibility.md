# Accessibility (customers)

- **Autofocus**: the landing page puts the cursor in the search box
  on load. The results header deliberately does not re-focus, so
  screen-reader and keyboard users keep their place in results and
  pagination.
- **Voice input**: the microphone button next to Search transcribes
  one utterance into the box via the built-in Web Speech API
  (Chrome/Edge/Safari). It appears only where supported; elsewhere
  the form is unchanged. The transcript fills the box for review —
  it never auto-submits, so misrecognitions are correctable.
- **Keyboard**: the suggestion combobox follows the standard map —
  arrows move, Enter picks, Escape closes. Results, disclosures, and
  pagination are native buttons and links.
- **Screen readers**: results, perspectives, diagnostics, and chat
  answers use labelled sections and headings; loading and result
  counts are announced through live regions; icon-only buttons all
  carry accessible names.
