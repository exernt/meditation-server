ALTER TABLE study
    RENAME COLUMN script_generation_parameters TO control_script_generation_parameters;
-- migrate:split
ALTER TABLE study
    ADD COLUMN experimental_script_generation_parameters text NOT NULL DEFAULT '';
-- migrate:split
UPDATE study
SET experimental_script_generation_parameters = control_script_generation_parameters;
