-- NameCheck persistence model. Invented for testing; no real system uses it.

CREATE TABLE screening_case (
    case_id        VARCHAR(32)  NOT NULL,
    submitted_name VARCHAR(200) NOT NULL,
    status         VARCHAR(16)  NOT NULL DEFAULT 'pending',
    reviewer_id    VARCHAR(32),
    created_at     TIMESTAMP    NOT NULL,
    CONSTRAINT pk_screening_case PRIMARY KEY (case_id),
    CONSTRAINT ck_screening_status CHECK (status IN ('pending','cleared','review','rejected'))
);

CREATE INDEX idx_screening_case_status ON screening_case (status, created_at);

CREATE TABLE watch_list_entry (
    entry_id    VARCHAR(32)  NOT NULL PRIMARY KEY,
    match_name  VARCHAR(200) NOT NULL,
    source      VARCHAR(64)  NOT NULL
);

CREATE VIEW open_screening_case AS
    SELECT case_id, submitted_name, created_at
    FROM screening_case
    WHERE status IN ('pending', 'review');

-- Vendor-specific syntax the general parser is not expected to structure. It
-- must be kept verbatim with its exact line range, not dropped or guessed at.
CREATE OR REPLACE PACKAGE BODY namecheck_admin IS
    PROCEDURE purge_cases(p_before IN DATE) IS
    BEGIN
        DELETE FROM screening_case WHERE created_at < p_before;
    END purge_cases;
END namecheck_admin;
