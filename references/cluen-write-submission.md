# Write Query Submission: Interview Activity Logging

## 1. Summary

Three SQL workflows for logging scheduled interviews to Encore as Activities:

- **Insert** when a new interview is confirmed.
- **Update** when a logged interview is rescheduled.
- **Cancel** when a logged interview is cancelled. The activity is retained for audit.

Each workflow runs in a single transaction. On any failure, the whole thing rolls back. No orphan activities, no partial link sets, no half-applied reschedules.

## 2. Credentials request

A distinct SQL user with write access, separate from the existing read-only credential. Same IP whitelist.

The write workflows are separated from read-workflows and are isolated to a small subset that is heavily checked, whereas LLMs may run read workflows at will. Hence the reason for separate credentials.

## 3. Runner and parameterization

- Driver: `pymssql` (Microsoft SQL Server driver, libtds underneath).
- Every input value is bound. The free-text fields are `@interview_description` and `@cancellation_note`, both landing in `GenieNotes`.

## 4. Workflow: insert a scheduled interview

```sql
BEGIN TRY
    BEGIN TRANSACTION;

    DECLARE @NewGenie TABLE (GUID UNIQUEIDENTIFIER);

    -- 1. The activity.
    INSERT INTO tGenie (
        StartTime,
        Duration,
        GenieNotes,
        GenieTypeGUID,
        UserEnteredGUID,
        StartTimeUTC,
        ActivityDTO,
        GenieDateString
    )
    OUTPUT inserted.GUID INTO @NewGenie
    VALUES (
        @interview_start_time,
        @interview_duration,
        @interview_description,
        @interview_type_guid,
        @user_guid,
        @interview_start_time_utc,
        @interview_start_dto,
        @interview_date_string
    );

    DECLARE @genie_guid UNIQUEIDENTIFIER =
        (SELECT GUID FROM @NewGenie);

    -- 2. Links. Minimum two: candidate and search.
    --    A third row is appended when the hiring company is in scope.
    INSERT INTO tGenieLink (GenieGUID, CPSUGUID, GenieLabelGUID, Confirmed)
    VALUES
        (@genie_guid, @candidate_guid, @candidate_role_label_guid, 0),
        (@genie_guid, @search_guid,    @search_role_label_guid,    0);

    -- 3. Backfill so Encore handles the audit string and cache.
    INSERT INTO tBackfill (RecordGUID, RecordType, DateAdded, UserGUID, IsNew)
    VALUES (@genie_guid, 4, GETDATE(), @user_guid, 1);

    COMMIT TRANSACTION;

    SELECT @genie_guid AS GenieGUID;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;
```

## 5. Workflow: update a rescheduled interview

The `tGenie.GUID` is captured at insert time and stored in our database, keyed to the scheduling thread. On reschedule, it is looked up and passed in.

Only the fields that move on a reschedule are updated: timing and notes. `UserEnteredGUID`, `GenieTypeGUID`, and link rows are not touched. The audit trail of who performed the reschedule is carried by the `tBackfill` row's `UserGUID`. `IsNew = 0`.

```sql
BEGIN TRY
    BEGIN TRANSACTION;

    UPDATE tGenie
    SET StartTime       = @interview_start_time,
        Duration        = @interview_duration,
        GenieNotes      = @interview_description,
        StartTimeUTC    = @interview_start_time_utc,
        ActivityDTO     = @interview_start_dto,
        GenieDateString = @interview_date_string
    WHERE GUID = @genie_guid;

    INSERT INTO tBackfill (RecordGUID, RecordType, DateAdded, UserGUID, IsNew)
    VALUES (@genie_guid, 4, GETDATE(), @user_guid, 0);

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;
```

## 6. Workflow: cancel a logged interview

The activity is retained. `GenieTypeGUID` is swapped to LRP's "Cancelled Interview" variant in `tGenieType` (resolved from the same config map used for `@interview_type_guid` on insert) and a cancellation note is written to `GenieNotes`. Time fields and links are not touched. `tBackfill` is written with `IsNew = 0`.

There is currently no "Cancelled Interview" Genie Type. I'd like to configure one for LRP's instance. I think it's cleaner than trying to delete all the records.

```sql
BEGIN TRY
    BEGIN TRANSACTION;

    UPDATE tGenie
    SET GenieTypeGUID = @cancelled_type_guid,
        GenieNotes    = @cancellation_note
    WHERE GUID = @genie_guid;

    INSERT INTO tBackfill (RecordGUID, RecordType, DateAdded, UserGUID, IsNew)
    VALUES (@genie_guid, 4, GETDATE(), @user_guid, 0);

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;
```

## 7. Field derivations

### 7.1 `tGenie`

| Column | Type | Source |
|---|---|---|
| `StartTime` | `datetime` | Interview start in the user's local time, no offset. |
| `Duration` | `int` | Minutes. |
| `GenieNotes` | `nvarchar` | Notes typed by the coordinator. Bound parameter. |
| `GenieTypeGUID` | `uniqueidentifier` | LRP-configured type from `tGenieType`. Config map in our service. On cancel, swapped to the "Cancelled Interview" variant. |
| `UserEnteredGUID` | `uniqueidentifier` | Coordinator's `tUser.GUID`, resolved from email via `tUserEmail`. Set on insert, not touched on update or cancel. |
| `StartTimeUTC` | `datetime` | `StartTime` converted to UTC using the user's IANA timezone. Computed in Python. |
| `ActivityDTO` | `datetimeoffset` | Same instant as `StartTime` with the user's local offset. A 1 PM Eastern interview on 2026-06-01 binds as `2026-06-01 13:00:00.000000 -04:00`. Computed in Python so DST is handled across future dates. |
| `GenieDateString` | `varchar(8)` | `MMDDYYYY` of the local interview date. Computed in Python as `dt.strftime("%m%d%Y")`. |

`GUID`, `Urgent`, `Done`, `TimeEntered` are left to column defaults on insert and not touched on update or cancel.

### 7.2 `tGenieLink`

| Column | Source |
|---|---|
| `GenieGUID` | New activity GUID, captured by `OUTPUT inserted.GUID`. |
| `CPSUGUID` | Entity being attached. Minimum set: candidate person GUID and search GUID. When the scheduling thread is anchored to a hiring company, a company link is added. |
| `GenieLabelGUID` | Role label from `tGenieLabel`. Static map in our service from internal role (candidate, search, company) to the corresponding `tGenieLabel.GUID`. No dynamic derivation, no user input. |
| `Confirmed` | `0`. Not auto-confirmed. |

### 7.3 `tBackfill`

| Column | Source |
|---|---|
| `RecordGUID` | The `tGenie.GUID`. New on insert, existing on update and cancel. |
| `RecordType` | `4` (Activity). |
| `DateAdded` | `GETDATE()`. |
| `UserGUID` | Coordinator performing the operation. Resolved via `tUserEmail`. |
| `IsNew` | `1` on insert, `0` on update and cancel. |
| `DateCompleted` | Omitted, per dev team note. |

