-- Link a queue entry directly to the wc_firms row it resolved to. Needed
-- because the user's typed input (firm_name_input) can differ from the
-- matched canonical company name (that's the whole point of fuzzy/LLM
-- matching) — re-deriving the link by name match at drain time is unsafe.
ALTER TABLE wc_queue ADD COLUMN IF NOT EXISTS firm_id INT REFERENCES wc_firms(id);
