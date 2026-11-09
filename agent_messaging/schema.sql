CREATE TABLE IF NOT EXISTS projects (
    projectKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    name VARCHAR(255) NOT NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS agents (
    projectKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    agentId VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    name VARCHAR(255) NOT NULL,
    sessionId VARCHAR(255) NOT NULL,
    runtimeSessionId VARCHAR(255) DEFAULT NULL,
    hostKey VARCHAR(128) NOT NULL DEFAULT '',
    runtime VARCHAR(32) NOT NULL DEFAULT 'mailbox',
    declaredState VARCHAR(32) NOT NULL DEFAULT 'active',
    task VARCHAR(255) NOT NULL DEFAULT '',
    note TEXT NOT NULL,
    createdAt VARCHAR(32) NOT NULL,
    updatedAt VARCHAR(32) NOT NULL,
    observedState VARCHAR(32) NOT NULL DEFAULT 'unknown',
    observedAt VARCHAR(32) DEFAULT NULL,
    PRIMARY KEY (projectKey, agentId),
    FOREIGN KEY (projectKey) REFERENCES projects(projectKey)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS aliases (
    projectKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    alias VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    agentId VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    updatedAt VARCHAR(32) NOT NULL,
    PRIMARY KEY (projectKey, alias),
    FOREIGN KEY (projectKey, agentId) REFERENCES agents(projectKey, agentId)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS messages (
    sequence BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
    projectKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    senderProject VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    senderId VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    recipient VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    subject VARCHAR(1000) NOT NULL,
    kind VARCHAR(255) NOT NULL,
    task VARCHAR(255) NOT NULL DEFAULT '',
    replyTo VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
    body MEDIUMTEXT NOT NULL,
    rawText MEDIUMTEXT NOT NULL,
    bodySHA256 CHAR(64) CHARACTER SET ascii NOT NULL,
    createdAt VARCHAR(32) NOT NULL,
    idempotencyKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
    requestSHA256 CHAR(64) CHARACTER SET ascii DEFAULT NULL,
    legacyPath VARCHAR(1000) DEFAULT NULL,
    legacyId VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
    metadataWarning TEXT DEFAULT NULL,
    UNIQUE KEY retryKey (senderProject, senderId, idempotencyKey),
    KEY inbox (projectKey, recipient, sequence),
    KEY legacyMessage (legacyId),
    KEY legacySource (projectKey, legacyPath(600)),
    FOREIGN KEY (projectKey) REFERENCES projects(projectKey)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS receipts (
    messageId VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    projectKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    agentId VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    addressedAs VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    acknowledgedAt VARCHAR(32) NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (messageId, projectKey, agentId),
    FOREIGN KEY (messageId) REFERENCES messages(id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS deliveries (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    messageId VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    projectKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    agentId VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'queued',
    attemptId VARCHAR(64) DEFAULT NULL,
    attemptedAt VARCHAR(32) DEFAULT NULL,
    nextAttemptAt DOUBLE NOT NULL DEFAULT 0,
    detail VARCHAR(1000) NOT NULL DEFAULT '',
    turnId VARCHAR(255) DEFAULT NULL,
    UNIQUE KEY recipientDelivery (messageId, projectKey, agentId),
    KEY pending (state, nextAttemptAt, id),
    FOREIGN KEY (messageId) REFERENCES messages(id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS credentials (
    tokenHash CHAR(64) CHARACTER SET ascii PRIMARY KEY,
    projectKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
    agentId VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
    hostKey VARCHAR(128) DEFAULT NULL,
    role VARCHAR(32) NOT NULL,
    createdAt VARCHAR(32) NOT NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS events (
    sequence BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    projectKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    kind VARCHAR(64) NOT NULL,
    reference VARCHAR(255) NOT NULL,
    createdAt VARCHAR(32) NOT NULL,
    KEY projectEvents (projectKey, sequence)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS migrationSources (
    sourceKey VARCHAR(1000) NOT NULL,
    sourceSHA256 CHAR(64) CHARACTER SET ascii NOT NULL,
    importedAt VARCHAR(32) NOT NULL,
    PRIMARY KEY (sourceKey(700), sourceSHA256)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS documents (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    projectKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    sourcePath VARCHAR(512) NOT NULL,
    sha256 CHAR(64) CHARACTER SET ascii NOT NULL,
    content LONGBLOB NOT NULL,
    createdAt VARCHAR(32) NOT NULL,
    UNIQUE KEY sourceVersion (projectKey,sourcePath,sha256),
    FOREIGN KEY (projectKey) REFERENCES projects(projectKey)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS actionItems (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    projectKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    actionKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    authorId VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    sourceReference VARCHAR(2000) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'needs-input',
    resolution TEXT NOT NULL,
    version INT UNSIGNED NOT NULL DEFAULT 1,
    responseMessageId VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
    createdAt VARCHAR(32) NOT NULL,
    updatedAt VARCHAR(32) NOT NULL,
    UNIQUE KEY projectAction (projectKey,actionKey),
    KEY actionPage (status,id),
    FOREIGN KEY (projectKey) REFERENCES projects(projectKey),
    FOREIGN KEY (responseMessageId) REFERENCES messages(id)
) ENGINE=InnoDB;
