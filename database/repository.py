import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from core.models import DeploymentRecord, DeploymentStatus, ApprovalStatus, VerificationStatus
from database.db import get_db_connection
from core.exceptions import DatabaseError, ApprovalError


class DeploymentRepository:
    """Handles CRUD operations for deployments against SQLite."""

    def __init__(self, db_path: Optional[Any] = None):
        self.db_path = db_path

    def _get_conn(self):
        return get_db_connection(self.db_path)

    def create(self, record: DeploymentRecord) -> DeploymentRecord:
        """Inserts a new deployment record."""
        conn = self._get_conn()
        query = """
        INSERT INTO deployments (
            deployment_id, user_request, service, status, approval_status,
            plan_hash, parsed_intent, normalized_spec, plan_summary,
            aws_resource_ids, verification_status, verification_details,
            error_message, aws_account_id, aws_identity_arn, aws_region,
            execution_mode, credential_source_type, approved_at,
            execution_started_at, execution_completed_at, terraform_exit_code,
            execution_logs, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                conn.execute(query, (
                    record.deployment_id,
                    record.user_request,
                    record.service,
                    record.status.value,
                    record.approval_status.value,
                    record.plan_hash,
                    json.dumps(record.parsed_intent) if record.parsed_intent is not None else None,
                    json.dumps(record.normalized_spec) if record.normalized_spec is not None else None,
                    json.dumps(record.plan_summary) if record.plan_summary is not None else None,
                    json.dumps(record.aws_resource_ids) if record.aws_resource_ids is not None else None,
                    record.verification_status.value,
                    json.dumps(record.verification_details) if record.verification_details is not None else None,
                    record.error_message,
                    record.aws_account_id,
                    record.aws_identity_arn,
                    record.aws_region,
                    record.execution_mode or "mock",
                    record.credential_source_type,
                    record.approved_at.isoformat() if record.approved_at else None,
                    record.execution_started_at.isoformat() if record.execution_started_at else None,
                    record.execution_completed_at.isoformat() if record.execution_completed_at else None,
                    record.terraform_exit_code,
                    record.execution_logs,
                    now,
                    now
                ))
            return record
        except Exception as e:
            raise DatabaseError(f"Failed to create deployment record {record.deployment_id}: {e}") from e
        finally:
            conn.close()

    def get(self, deployment_id: str) -> Optional[DeploymentRecord]:
        """Fetches a deployment record by ID."""
        conn = self._get_conn()
        query = "SELECT * FROM deployments WHERE deployment_id = ?"
        try:
            cursor = conn.cursor()
            cursor.execute(query, (deployment_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_record(row)
        except Exception as e:
            raise DatabaseError(f"Failed to fetch deployment {deployment_id}: {e}") from e
        finally:
            conn.close()

    def list_recent(self, limit: int = 20) -> List[DeploymentRecord]:
        """Lists recent deployments."""
        conn = self._get_conn()
        query = "SELECT * FROM deployments ORDER BY created_at DESC LIMIT ?"
        try:
            cursor = conn.cursor()
            cursor.execute(query, (limit,))
            rows = cursor.fetchall()
            return [self._row_to_record(row) for row in rows]
        except Exception as e:
            raise DatabaseError(f"Failed to list recent deployments: {e}") from e
        finally:
            conn.close()

    def update_status(
        self,
        deployment_id: str,
        status: DeploymentStatus,
        error_message: Optional[str] = None
    ) -> bool:
        """Updates deployment status."""
        conn = self._get_conn()
        query = """
        UPDATE deployments
        SET status = ?, error_message = ?, updated_at = ?
        WHERE deployment_id = ?
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                cursor = conn.execute(query, (status.value, error_message, now, deployment_id))
                return cursor.rowcount > 0
        except Exception as e:
            raise DatabaseError(f"Failed to update status for {deployment_id}: {e}") from e
        finally:
            conn.close()

    def update_approval(
        self,
        deployment_id: str,
        approval_status: ApprovalStatus,
        plan_hash: Optional[str] = None,
        aws_account_id: Optional[str] = None,
        aws_region: Optional[str] = None,
        normalized_spec: Optional[Dict[str, Any]] = None,
        plan_summary: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Updates plan hash, approval status, target AWS binding, and optionally specification."""
        existing = self.get(deployment_id)
        if not existing:
            return False

        # Terminal state guard: prevent mutating approval on deployments that already executed
        if existing.status in (DeploymentStatus.COMPLETED, DeploymentStatus.PARTIALLY_DEPLOYED):
            raise ApprovalError(
                f"Cannot mutate approval status of terminal deployment '{deployment_id}' "
                f"with status '{existing.status.value}'."
            )

        if approval_status == ApprovalStatus.APPROVED:
            if not plan_hash or not plan_hash.strip():
                raise ApprovalError(f"Cannot approve deployment '{deployment_id}' with an empty or missing plan_hash.")

        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()

        # Preserve genuine approved_at timestamp if one was previously recorded;
        # record now if newly approved; do not fabricate if None.
        existing_approved_at = (
            existing.approved_at.isoformat()
            if hasattr(existing.approved_at, "isoformat")
            else (str(existing.approved_at) if existing.approved_at else None)
        )
        if approval_status == ApprovalStatus.APPROVED:
            approved_at = existing_approved_at or now
        else:
            approved_at = existing_approved_at

        query = """
        UPDATE deployments
        SET approval_status = ?,
            plan_hash = COALESCE(?, plan_hash),
            aws_account_id = COALESCE(?, aws_account_id),
            aws_region = COALESCE(?, aws_region),
            normalized_spec = COALESCE(?, normalized_spec),
            plan_summary = COALESCE(?, plan_summary),
            approved_at = ?,
            updated_at = ?
        WHERE deployment_id = ?
        """
        try:
            with conn:
                cursor = conn.execute(query, (
                    approval_status.value,
                    plan_hash,
                    aws_account_id,
                    aws_region,
                    json.dumps(normalized_spec) if normalized_spec is not None else None,
                    json.dumps(plan_summary) if plan_summary is not None else None,
                    approved_at,
                    now,
                    deployment_id
                ))
                return cursor.rowcount > 0
        except Exception as e:
            if isinstance(e, ApprovalError):
                raise
            raise DatabaseError(f"Failed to update approval for {deployment_id}: {e}") from e
        finally:
            conn.close()

    def update_normalized_spec(
        self,
        deployment_id: str,
        normalized_spec: Dict[str, Any],
        plan_summary: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Updates normalized_spec and plan_summary for a deployment record."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        query = """
        UPDATE deployments
        SET normalized_spec = ?,
            plan_summary = COALESCE(?, plan_summary),
            updated_at = ?
        WHERE deployment_id = ?
        """
        try:
            with conn:
                cursor = conn.execute(query, (
                    json.dumps(normalized_spec),
                    json.dumps(plan_summary) if plan_summary is not None else None,
                    now,
                    deployment_id,
                ))
                return cursor.rowcount > 0
        except Exception as e:
            raise DatabaseError(f"Failed to update normalized_spec for {deployment_id}: {e}") from e
        finally:
            conn.close()

    def update_deployment_record(
        self,
        deployment_id: str,
        status: Optional[DeploymentStatus] = None,
        aws_resource_ids: Optional[List[str]] = None,
        normalized_spec: Optional[Dict[str, Any]] = None,
        plan_summary: Optional[Dict[str, Any]] = None,
        terraform_exit_code: Optional[int] = None,
        error_message: Optional[str] = None,
        execution_logs: Optional[str] = None,
    ) -> bool:
        """Updates mutable deployment record fields safely."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        updates = ["updated_at = ?"]
        params: List[Any] = [now]

        if status is not None:
            updates.append("status = ?")
            params.append(status.value)
        if aws_resource_ids is not None:
            updates.append("aws_resource_ids = ?")
            params.append(json.dumps(aws_resource_ids))
        if normalized_spec is not None:
            updates.append("normalized_spec = ?")
            params.append(json.dumps(normalized_spec))
        if plan_summary is not None:
            updates.append("plan_summary = ?")
            params.append(json.dumps(plan_summary))
        if terraform_exit_code is not None:
            updates.append("terraform_exit_code = ?")
            params.append(terraform_exit_code)
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)
        if execution_logs is not None:
            updates.append("execution_logs = ?")
            params.append(execution_logs)

        params.append(deployment_id)
        query = f"UPDATE deployments SET {', '.join(updates)} WHERE deployment_id = ?"

        try:
            with conn:
                cursor = conn.execute(query, tuple(params))
                return cursor.rowcount > 0
        except Exception as e:
            raise DatabaseError(f"Failed to update deployment record {deployment_id}: {e}") from e
        finally:
            conn.close()

    def record_execution_start(
        self,
        deployment_id: str,
        execution_mode: str = "mock",
        aws_account_id: Optional[str] = None,
        aws_identity_arn: Optional[str] = None,
        aws_region: Optional[str] = None,
        credential_source_type: Optional[str] = None,
    ) -> bool:
        """Records the start of a deployment execution for audit trails."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        query = """
        UPDATE deployments
        SET status = ?,
            execution_mode = ?,
            aws_account_id = COALESCE(?, aws_account_id),
            aws_identity_arn = ?,
            aws_region = COALESCE(?, aws_region),
            credential_source_type = ?,
            execution_started_at = ?,
            updated_at = ?
        WHERE deployment_id = ?
        """
        try:
            with conn:
                cursor = conn.execute(query, (
                    DeploymentStatus.IN_PROGRESS.value,
                    execution_mode,
                    aws_account_id,
                    aws_identity_arn,
                    aws_region,
                    credential_source_type,
                    now,
                    now,
                    deployment_id,
                ))
                return cursor.rowcount > 0
        except Exception as e:
            raise DatabaseError(f"Failed to record execution start for {deployment_id}: {e}") from e
        finally:
            conn.close()

    def record_execution_complete(
        self,
        deployment_id: str,
        status: DeploymentStatus,
        exit_code: Optional[int] = None,
        aws_resource_ids: Optional[List[str]] = None,
        execution_logs: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """Records execution termination, exit code, resource IDs, and sanitized logs."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        query = """
        UPDATE deployments
        SET status = ?,
            terraform_exit_code = ?,
            aws_resource_ids = COALESCE(?, aws_resource_ids),
            execution_logs = ?,
            error_message = ?,
            execution_completed_at = ?,
            updated_at = ?
        WHERE deployment_id = ?
        """
        try:
            with conn:
                cursor = conn.execute(query, (
                    status.value,
                    exit_code,
                    json.dumps(aws_resource_ids) if aws_resource_ids is not None else None,
                    execution_logs,
                    error_message,
                    now,
                    now,
                    deployment_id,
                ))
                return cursor.rowcount > 0
        except Exception as e:
            raise DatabaseError(f"Failed to record execution completion for {deployment_id}: {e}") from e
        finally:
            conn.close()

    def record_verification_report(
        self,
        deployment_id: str,
        verification_status: VerificationStatus,
        verification_details: Dict[str, Any],
    ) -> bool:
        """Persists the independent live AWS resource verification report and status."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        query = """
        UPDATE deployments
        SET verification_status = ?,
            verification_details = ?,
            updated_at = ?
        WHERE deployment_id = ?
        """
        try:
            with conn:
                cursor = conn.execute(query, (
                    verification_status.value,
                    json.dumps(verification_details),
                    now,
                    deployment_id,
                ))
                return cursor.rowcount > 0
        except Exception as e:
            raise DatabaseError(f"Failed to record verification report for {deployment_id}: {e}") from e
        finally:
            conn.close()

    def count_summary(self) -> Dict[str, int]:
        """Returns deployment summary counts for the dashboard."""
        conn = self._get_conn()
        query = """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as successful,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress
        FROM deployments
        """
        try:
            cursor = conn.cursor()
            cursor.execute(query)
            row = cursor.fetchone()
            if not row:
                return {"total": 0, "successful": 0, "failed": 0, "in_progress": 0}
            return {
                "total": row["total"] or 0,
                "successful": row["successful"] or 0,
                "failed": row["failed"] or 0,
                "in_progress": row["in_progress"] or 0,
            }
        except Exception as e:
            raise DatabaseError(f"Failed to fetch summary stats: {e}") from e
        finally:
            conn.close()

    def add_chat_message(
        self,
        message_id: str,
        role: str,
        content: str,
        deployment_id: Optional[str] = None,
    ) -> bool:
        """Stores a unique chat message to SQLite. Rejects duplicates via PRIMARY KEY."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        query = """
        INSERT OR IGNORE INTO chat_messages (message_id, role, content, deployment_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """
        try:
            with conn:
                cursor = conn.execute(query, (message_id, role, content, deployment_id, now))
                return cursor.rowcount > 0
        except Exception as e:
            raise DatabaseError(f"Failed to persist chat message '{message_id}': {e}") from e
        finally:
            conn.close()

    def list_chat_messages(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Returns persisted chat messages in chronological order."""
        conn = self._get_conn()
        query = "SELECT message_id, role, content, deployment_id, created_at FROM chat_messages ORDER BY created_at ASC LIMIT ?"
        try:
            cursor = conn.cursor()
            cursor.execute(query, (limit,))
            rows = cursor.fetchall()
            return [
                {
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "deployment_id": row["deployment_id"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        except Exception as e:
            raise DatabaseError(f"Failed to list chat messages: {e}") from e
        finally:
            conn.close()

    def clear_chat_messages(self) -> bool:
        """Clears all persisted chat messages (useful for tests or user reset)."""
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("DELETE FROM chat_messages")
                return True
        except Exception as e:
            raise DatabaseError(f"Failed to clear chat messages: {e}") from e
        finally:
            conn.close()

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieves a persisted system setting value by key."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM system_settings WHERE key = ?;", (key,))
            row = cursor.fetchone()
            if row:
                return row["value"]
            return default
        except Exception:
            return default
        finally:
            conn.close()

    def set_setting(self, key: str, value: str) -> None:
        """Persists or updates a system setting value."""
        conn = self._get_conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP;
                    """,
                    (key, value),
                )
        except Exception as e:
            raise DatabaseError(f"Failed to persist setting '{key}': {e}") from e
        finally:
            conn.close()

    def _row_to_record(self, row: Any) -> DeploymentRecord:
        """Converts an SQLite row into a typed DeploymentRecord."""
        keys = row.keys() if hasattr(row, "keys") else []
        parsed_intent = json.loads(row["parsed_intent"]) if "parsed_intent" in keys and row["parsed_intent"] else None
        normalized_spec = json.loads(row["normalized_spec"]) if "normalized_spec" in keys and row["normalized_spec"] else None
        plan_summary = json.loads(row["plan_summary"]) if "plan_summary" in keys and row["plan_summary"] else None
        aws_resource_ids = json.loads(row["aws_resource_ids"]) if "aws_resource_ids" in keys and row["aws_resource_ids"] else []
        verification_details = json.loads(row["verification_details"]) if "verification_details" in keys and row["verification_details"] else None

        def parse_dt(val: Any) -> Optional[datetime]:
            if not val:
                return None
            if isinstance(val, datetime):
                return val
            try:
                return datetime.fromisoformat(str(val))
            except Exception:
                return None

        created = parse_dt(row["created_at"]) if "created_at" in keys else datetime.now(timezone.utc)
        updated = parse_dt(row["updated_at"]) if "updated_at" in keys else datetime.now(timezone.utc)
        approved_at = parse_dt(row["approved_at"]) if "approved_at" in keys else None
        execution_started_at = parse_dt(row["execution_started_at"]) if "execution_started_at" in keys else None
        execution_completed_at = parse_dt(row["execution_completed_at"]) if "execution_completed_at" in keys else None

        return DeploymentRecord(
            deployment_id=row["deployment_id"],
            user_request=row["user_request"],
            service=row["service"],
            status=DeploymentStatus(row["status"]),
            approval_status=ApprovalStatus(row["approval_status"]),
            plan_hash=row["plan_hash"] if "plan_hash" in keys else None,
            parsed_intent=parsed_intent,
            normalized_spec=normalized_spec,
            plan_summary=plan_summary,
            aws_resource_ids=aws_resource_ids,
            verification_status=VerificationStatus(row["verification_status"]) if "verification_status" in keys else VerificationStatus.NOT_CHECKED,
            verification_details=verification_details,
            error_message=row["error_message"] if "error_message" in keys else None,
            aws_account_id=row["aws_account_id"] if "aws_account_id" in keys else None,
            aws_identity_arn=row["aws_identity_arn"] if "aws_identity_arn" in keys else None,
            aws_region=row["aws_region"] if "aws_region" in keys else None,
            execution_mode=row["execution_mode"] if "execution_mode" in keys and row["execution_mode"] else "mock",
            credential_source_type=row["credential_source_type"] if "credential_source_type" in keys else None,
            approved_at=approved_at,
            execution_started_at=execution_started_at,
            execution_completed_at=execution_completed_at,
            terraform_exit_code=row["terraform_exit_code"] if "terraform_exit_code" in keys else None,
            execution_logs=row["execution_logs"] if "execution_logs" in keys else None,
            created_at=created or datetime.now(timezone.utc),
            updated_at=updated or datetime.now(timezone.utc),
        )

