# NameCheck Requirements

The NameCheck service screens submitted names against a watch list. Everything in
this file is invented for testing; it describes no real system.

## 2. Scope

NameCheck runs as part of the onboarding flow. It is reached over HTTP and it
publishes an audit event for every decision.

## 4. Functional Requirements

### 4.3 Manual review

REQ-NC-017: a manual review must time out after 30 minutes, and the case must
return to the shared review queue when it does.

REQ-NC-018: the client retries a failed screening call three times before it
reports an error to the caller.

- The screening endpoint is POST /name-check.
- The result endpoint is GET /name-check/{caseId}.
- The review timeout is configured by `namecheck.review.timeout.minutes`.
- Decisions are published to the `namecheck.decisions` topic.

```java
public final class NameCheckService {
    public ScreeningResult screen(ScreeningRequest request) {
        return watchList.match(request.normalizedName());
    }
}
```

### 4.4 Error codes

| Code | Meaning | Retryable |
| --- | --- | --- |
| NC-100 | The submitted name was empty | no |
| NC-101 | The watch list was unavailable | yes |
| NC-102 | The review timed out | yes |

> Ticket ABC-1234 tracks the rollout of the NC-102 retry behaviour.

## 5. Data

The `screening_case` table stores one row per screening decision. See
`sample-schema.sql` for its definition.
