# Hosted record attachments

## Result

The [reviewed CNBC record](https://ciss-advertising-observatory-production.up.railway.app/records/d340f887-efa7-5746-aaf8-14aabba6b63f)
now shows its stored text, original link, PDF snapshot and first-page preview.
One PDF and one PNG were transferred privately to the application's persistent
Railway volume. The application version remains **0.4.7**.

## Verification

- The installed package checked the complete mounted bundle against its pinned
  manifest SHA-256, including both file hashes.
- Checks in two deployments with different replica IDs confirmed that the
  selected bundle survived replacement of the running container.
- The [hosted HTTP report](record_assets_http_hosted_20260930.json) independently
  checks the returned article body, PDF, downloaded PDF and PNG against the
  private bundle. Their bytes and types matched.
- Unknown assets and the same asset under a different existing public record
  returned 404 without leaking the selected PDF or PNG bytes.
- The [local HTTP check](record_assets_http_local_20260930.json) also passed.
- Browser inspection showed the source controls and a rendered preview. The
  download endpoint was tested over HTTP; a separate browser download action
  was not tested.

The [deployment receipt](record_assets_hosted_20260930.json) records the mounted
checks, deployment identities, report hashes and unchanged source/index
identifiers. Screenshots and private source files remain outside Git.

## Scope

This covers **one selected engineering-reviewed source capture**, not complete
archive coverage or customer acceptance. The capture has partial text and an
untranscribed infographic. It does not establish the current original page's
availability or the truth of the advertiser's statements.

No CLAIMS source identity or classification was approved or published. Real
social data and independent client review remain pending. No model was called
for these deployment checks.

For repeatable operation, see [attachment deployment and HTTP checks](../docs/record_assets.md).

The checker passed 76 new offline engineering tests; the combined checker,
bundle and record-route tests passed 143 cases with one skipped case. These
synthetic checks verify failure handling, not answer quality or client acceptance.
