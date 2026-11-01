from alembic import op

revision = '001'
down_revision = None

def upgrade():
    op.execute("""CREATE TABLE control (
	id SERIAL NOT NULL, 
	paused BOOLEAN, 
	cooldown TIMESTAMP WITH TIME ZONE, 
	telegram_due TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
)""")
    op.execute("""CREATE TABLE listings (
	id TEXT NOT NULL, 
	data JSON NOT NULL, 
	first_seen TIMESTAMP WITH TIME ZONE, 
	last_seen TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
)""")
    op.execute("""CREATE TABLE parser_runs (
	id SERIAL NOT NULL, 
	key VARCHAR(64), 
	created TIMESTAMP WITH TIME ZONE, 
	elapsed_ms INTEGER, 
	found INTEGER, 
	error VARCHAR(200), 
	PRIMARY KEY (id)
)""")
    op.execute("""CREATE INDEX ix_parser_runs_created ON parser_runs (created)""")
    op.execute("""CREATE INDEX ix_parser_runs_key ON parser_runs (key)""")
    op.execute("""CREATE TABLE users (
	id BIGINT NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	interval INTEGER NOT NULL, 
	region VARCHAR(100), 
	currency VARCHAR(3), 
	created TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
)""")
    op.execute("""CREATE TABLE notification_outbox (
	user_id BIGINT NOT NULL, 
	listing_id TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	attempts INTEGER, 
	due TIMESTAMP WITH TIME ZONE, 
	sent TIMESTAMP WITH TIME ZONE, 
	message_id BIGINT, 
	PRIMARY KEY (user_id, listing_id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(listing_id) REFERENCES listings (id)
)""")
    op.execute("""CREATE INDEX ix_notification_outbox_due ON notification_outbox (due)""")
    op.execute("""CREATE INDEX ix_notification_outbox_status ON notification_outbox (status)""")
    op.execute("""CREATE TABLE searches (
	id SERIAL NOT NULL, 
	user_id BIGINT NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	url TEXT NOT NULL, 
	key VARCHAR(64) NOT NULL, 
	filters JSON NOT NULL, 
	interval INTEGER NOT NULL, 
	active BOOLEAN NOT NULL, 
	initialized TIMESTAMP WITH TIME ZONE, 
	success TIMESTAMP WITH TIME ZONE, 
	manual TIMESTAMP WITH TIME ZONE, 
	due TIMESTAMP WITH TIME ZONE NOT NULL, 
	failures INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)""")
    op.execute("""CREATE INDEX ix_searches_due ON searches (due)""")
    op.execute("""CREATE INDEX ix_searches_key ON searches (key)""")
    op.execute("""CREATE INDEX ix_searches_user_id ON searches (user_id)""")
    op.execute("""CREATE TABLE search_listings (
	search_id INTEGER NOT NULL, 
	listing_id TEXT NOT NULL, 
	"new" BOOLEAN NOT NULL, 
	created TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (search_id, listing_id), 
	FOREIGN KEY(search_id) REFERENCES searches (id) ON DELETE CASCADE, 
	FOREIGN KEY(listing_id) REFERENCES listings (id)
)""")
    op.execute("""CREATE INDEX ix_search_listings_created ON search_listings (created)""")
    op.execute("INSERT INTO control (id, paused) VALUES (1, false)")

def downgrade():
    op.drop_table('search_listings')
    op.drop_table('searches')
    op.drop_table('notification_outbox')
    op.drop_table('users')
    op.drop_table('parser_runs')
    op.drop_table('listings')
    op.drop_table('control')
