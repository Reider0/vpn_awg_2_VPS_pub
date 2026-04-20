DROP TABLE IF EXISTS stats;
DROP TABLE IF EXISTS devices;
DROP TABLE IF EXISTS user_tg_links;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS settings;
DROP TABLE IF EXISTS events_log;
DROP TABLE IF EXISTS support_tickets;

-- 1. Таблица пользователей
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    uuid TEXT NOT NULL UNIQUE,
    device TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    expires_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    first_connected_at TIMESTAMP
);

-- 1.5 Таблица привязок Telegram ID
CREATE TABLE user_tg_links (
    uuid TEXT REFERENCES users(uuid) ON DELETE CASCADE,
    tg_id BIGINT,
    UNIQUE(uuid, tg_id)
);

-- 2. Таблица статистики
CREATE TABLE stats (
    id SERIAL PRIMARY KEY,
    user_uuid TEXT REFERENCES users(uuid) ON DELETE CASCADE,
    bytes_in BIGINT DEFAULT 0,
    bytes_out BIGINT DEFAULT 0,
    last_seen TIMESTAMP
);

-- 3. Настройки
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- 4. Логи системы и безопасности
CREATE TABLE events_log (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT NOW(),
    event_type TEXT,
    message TEXT
);

-- 5. Обращения в техподдержку
CREATE TABLE support_tickets (
    id SERIAL PRIMARY KEY,
    user_uuid TEXT REFERENCES users(uuid) ON DELETE CASCADE,
    message TEXT,
    status TEXT DEFAULT 'open',
    created_at TIMESTAMP DEFAULT NOW()
);

-- ИНДЕКСЫ ДЛЯ ЗАЩИТЫ ОТ ПЕРЕГРУЗКИ ОЗУ (Out-Of-Memory)
CREATE INDEX idx_stats_user_seen ON stats(user_uuid, last_seen);
CREATE INDEX idx_events_timestamp ON events_log(timestamp);