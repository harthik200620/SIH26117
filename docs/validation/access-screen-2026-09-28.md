# Access screen and navigation review

28 September 2026. **Established fact:** this is a bounded source/browser review, not a WCAG compliance audit.

The app previously mounted the workspace while checking authentication and kept requesting protected health/task endpoints while locked. The entry path now waits for its initial access check, suspends polling while locked, clears recent-task labels on authentication loss and prevents duplicate unlock submissions. Failed-login text is associated with the password field and announced as an alert.

The navigation now identifies the active page, hides decorative icons from assistive technology and provides a keyboard skip link to the main workspace landmark. Focus outlines have a visible offset. Entry-screen text and input-border colors were darkened for readability.

**Measured checks:** the production TypeScript/Vite build passed. In the actual localhost browser, the locked screen displayed the expected label, password field and Unlock button without displaying the workspace. Tab moved first to the access-key field, then to Unlock; focus was visibly outlined. The screen was visually inspected at the browser's current viewport. No credential was entered or altered.

**Established fact:** relative-luminance calculations for the source CSS color pairs give 6.73:1 for entry helper text (`#526046` on white), 3.61:1 for the input border (`#7c886f` against `#fafbf8`) and 5.19:1 for the focus outline (`#597453` against white). These measurements cover those color pairs only, not every rendered state.

**Limits:** successful sign-in, authenticated skip-link activation, screen-reader speech, mobile layouts, 200% zoom, and complete contrast measurements were not exercised in this review. The full application must not be described as accessibility-certified based on these checks.
