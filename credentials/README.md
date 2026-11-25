# Local credentials (not for Git)

This folder is for local service-account files only. No usable credential is
included in this portfolio copy.

Obtain service-account JSON keys only through an authorized company process,
then place them locally using the filenames configured in `.env`, for example:

- `service-account.json` for BigQuery
- `google-sheets-service-account.json` for Google Sheets

Real credential files are ignored by Git and Docker. Never replace the example
template with a real key in a commit, issue, screenshot, or public repository.
For Gitea Actions, store the complete JSON contents in repository secrets
instead of committing the key file.
