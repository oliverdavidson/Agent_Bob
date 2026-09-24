# Bob

Bob is BridgeWerk's bookkeeping agent. People email documents to `bob@bridgewerk.ca`; Bob works out what each one is, codes it, and books it in QuickBooks Online. Payments are out of scope.

The plan, decisions and build order are in [docs/phase1-spec.md](docs/phase1-spec.md).

**Current state (slice 1):** mailbox ingestion and triage. Bob reads new mail, stores the originals, flags duplicates and classifies each attachment (invoice, receipt, statement, GL export and so on). Nothing is written to QuickBooks yet.

## Layout

```
bob/
  config.py        settings from BOB_* environment variables
  models.py        tables: inbound_emails, attachments, documents, jobs, audit_events
  jobs.py          Postgres work queue (SELECT ... FOR UPDATE SKIP LOCKED)
  audit.py         append-only audit events
  storage.py       original documents (local disk in dev, Blob Storage in Azure)
  mail/            Microsoft Graph mailbox client and ingestion
  agent/           Claude on Foundry: client, triage step, versioned prompts
  worker.py        background loop: poll mailbox, run jobs
  main.py          FastAPI app (health, status) that also runs the worker
migrations/        Alembic
infra/main.bicep   Azure resources
tests/
```

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env         # fill in
alembic upgrade head
uvicorn bob.main:app --reload
```

Tests run on SQLite by default. To run them against Postgres (which also covers the queue's row locking):

```bash
BOB_TEST_DATABASE_URL=postgresql+psycopg://user@localhost/bob_test pytest
```

## Deploying to Azure

1. **Infrastructure.**
   ```bash
   az group create -n rg-bob -l canadacentral
   az deployment group create -g rg-bob -f infra/main.bicep \
     -p foundryResource=<foundry-resource-name> postgresAdminPassword=<strong-password>
   ```
   Note the outputs: `identityPrincipalId`, `identityClientId`, `registry`.

2. **Image.**
   ```bash
   az acr build -r <registry> -t bob:$(git rev-parse --short HEAD) .
   az containerapp update -g rg-bob -n bob --image <registry>/bob:<tag>
   ```

3. **Mailbox access, limited to Bob's mailbox.** Create `bob@bridgewerk.ca` (a shared mailbox is enough). Do **not** grant `Mail.*` application permissions in Entra ID; those cover every mailbox in the tenant. Instead, in Exchange Online PowerShell:
   ```powershell
   New-ServicePrincipal -AppId <identityClientId> -ObjectId <identityPrincipalId> -DisplayName "Bob"
   New-ManagementScope -Name "Bob mailbox only" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'bob@bridgewerk.ca'"
   New-ManagementRoleAssignment -App <identityClientId> -Role "Application Mail.ReadWrite" -CustomResourceScope "Bob mailbox only"
   Test-ServicePrincipalAuthorization -Identity <identityClientId> -Resource bob@bridgewerk.ca
   ```
   `Application Mail.Send` is added the same way when the reply loop is built.

4. **Claude on Foundry.** Deploy a Claude model in the Foundry resource. Set `BOB_MODEL` to the deployment name if it differs from `claude-opus-5-5`. Give Bob's managed identity the role Foundry requires for Entra ID model calls on that resource (check Foundry's current docs; typically Azure AI User or Cognitive Services User):
   ```bash
   az role assignment create --assignee <identityPrincipalId> --role "<role>" --scope <foundry-resource-id>
   ```

5. **Check it.** `az containerapp logs show -g rg-bob -n bob --follow`, then send a test invoice to `bob@bridgewerk.ca`.

The Container App has no public ingress, since Bob polls the mailbox. It runs exactly one replica.
