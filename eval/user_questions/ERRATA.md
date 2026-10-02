# Errata for version 1

Version 1 stays frozen; corrections are listed here and will be applied in version 2.

| Case | Problem | Correct expectation | Affected results |
| --- | --- | --- | --- |
| U38 | `must_say` assumed disclosure wording is unavailable. Many article texts contain disclosure sentences, for example "The content is paid for and supplied by advertiser." | A good answer quotes those sentences with citations and says which ads it checked; it must not claim that every ad was disclosed unless every cited text shows it. | Run `user-questions-run-20261001T160015Z` (code d84910c): U38 must be reviewed on its cited evidence, not marked wrong for using disclosure text. |
