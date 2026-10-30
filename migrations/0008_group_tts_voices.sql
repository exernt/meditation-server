CREATE TYPE study_tts_voice AS ENUM ('male', 'female');

-- migrate:split
ALTER TABLE study
    ADD COLUMN control_tts_voice study_tts_voice NOT NULL DEFAULT 'male',
    ADD COLUMN experimental_tts_voice study_tts_voice NOT NULL DEFAULT 'male';
