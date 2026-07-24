# Compatibility gate

This fixture answers one question before the Roamer design is ported: can pinned
atopile `0.15.7` safely generate and re-synchronize a board that KiCad `10.0.3`
can consume?

The project uses the `UNI_ROYAL_0603WAF1003T5E` example part from atopile's
official repository at commit
`619eda7f777558a3e500dbad9cc2941712881495`. Vendoring that four-file fixture
keeps the gate deterministic and avoids runtime dependency on the component API.
