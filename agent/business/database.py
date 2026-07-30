"""Database facade: SQLite locally, MySQL when configured."""
from agent.business.config import load_business_config
if load_business_config().database_backend == 'mysql':
    from agent.business.mysql_database import *
else:
    from agent.business.sqlite_database import *
