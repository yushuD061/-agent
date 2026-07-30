-- Destructive rollback for migration 004 only. Back up and stop workers first.
DROP TABLE IF EXISTS ops_email_delivery_audit;
DROP TABLE IF EXISTS ops_email_delivery;

