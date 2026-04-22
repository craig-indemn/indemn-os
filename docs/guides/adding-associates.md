# Adding Associates

Associates are AI actors in the system. They claim messages from their queue, process work, and update entities -- just like human actors, but autonomous. This guide walks through creating, deploying, and tuning an associate from scratch.

---

## 1. Planning

Before writing any code, answer three questions:

**What will the associate do?** Define the scope narrowly. "Classify incoming emails" is good. "Handle all email operations" is too broad -- split it into classifier, responder, and router.

**Which entities does it operate on?** List the entity types it needs to read and write. This determines the role permissions. An email classifier needs read/write on `Email` and read on `Contact`.

**What mode?** Associates run in one of three modes:

| Mode | When to use | Cost |
|------|-------------|------|
| `deterministic` | Rules handle 100% of cases. No LLM calls. | Zero AI cost |
| `reasoning` | Every message requires LLM judgment. | Full AI cost per message |
| `hybrid` | Rules handle the common cases, LLM handles the rest. | AI cost only on `needs_reasoning` |

Start with `hybrid` unless you are certain rules can cover everything. The `--auto` pattern (described below) lets you add deterministic rules over time, reducing AI cost without changing the associate.

---

## 2. Writing the Skill

The skill is a markdown file that tells the associate how to do its job. It is the associate's instruction manual -- written in natural language, consumed by the LLM at runtime.

Create a file in your skills directory:

```markdown
# Email Classifier

You are an email classifier for GIC Underwriters.

## When You Receive Work
You'll receive emails that need classification. For each email:

1. Check the sender domain and subject line
2. Use `indemn email update <id> --auto classify` first
3. If --auto returns needs_reasoning, analyze the email content:
   - carrier_response: from a known carrier
   - new_submission: new business request
   - customer_inquiry: question from an existing customer
4. Set the classification: `indemn email update <id> --data '{"classification": "<type>"}'`
5. If unsure, set needs_review: `indemn email update <id> --data '{"needs_review": true}'`

## Classification Rules
- Emails from @travelers.com, @progressive.com, @libertymutual.com are always carrier_response
- Emails with "Quote Request" or "New Submission" in subject are new_submission
- Emails from known customer domains (check the Contact entity) are customer_inquiry
- Everything else requires reasoning

## Escalation
If the email content is ambiguous or contains multiple intents, set needs_review to true and add a note:
`indemn email update <id> --data '{"needs_review": true, "review_note": "<your reasoning>"}'`
```

Key principles for writing skills:

- **Be specific.** "Analyze the email" is vague. "Check sender domain against known carrier list" is actionable.
- **Define the `--auto` path first.** The deterministic rules should be the primary path. LLM reasoning is the fallback.
- **Include escalation criteria.** The associate must know when to stop and hand off to a human.
- **Reference entity operations by CLI command.** The associate executes these commands at runtime.

---

## 3. Creating the Skill

Register the skill with the platform:

```bash
indemn skill create --name email-classifier --content-from-file skills/email-classifier.md
```

To update an existing skill:

```bash
indemn skill update email-classifier --content-from-file skills/email-classifier.md
```

To verify it was created:

```bash
indemn skill get email-classifier
```

---

## 4. Runtimes

A runtime is the execution environment for an associate -- it defines the LLM provider, model, and configuration. Multiple associates can share a runtime.

**Check existing runtimes first:**

```bash
indemn runtime list
```

If a suitable runtime exists (same model, same provider), reuse it. Creating unnecessary runtimes wastes configuration effort and makes auditing harder.

**Create a new runtime when:**
- You need a different model (e.g., Haiku for low-cost classification vs Sonnet for complex reasoning)
- You need different temperature or token limits
- You need a dedicated runtime for cost tracking

```bash
indemn runtime create --data '{
  "name": "classifier-runtime",
  "provider": "anthropic",
  "model": "claude-sonnet-4-20250514",
  "temperature": 0.0,
  "max_tokens": 1024
}'
```

Note the returned runtime ID -- you will need it when creating the associate.

---

## 5. Creating the Associate

```bash
indemn actor create --type associate --name "Email Classifier" \
  --mode hybrid --runtime-id <runtime-id> --role email_classifier \
  --skills email-classifier
```

The role (`email_classifier`) must already exist with the appropriate permissions and watches. See [Adding Watches](adding-watches.md) for how to configure those.

To attach multiple skills:

```bash
indemn actor create --type associate --name "Submission Processor" \
  --mode hybrid --runtime-id <runtime-id> --role submission_handler \
  --skills "intake-validator,risk-scorer,carrier-matcher"
```

Verify the associate was created:

```bash
indemn actor get <associate-id>
```

---

## 6. Activating

Associates are created in `provisioned` state. They do not claim messages until activated:

```bash
indemn actor transition <associate-id> --to active
```

To suspend (stops claiming new messages, finishes in-progress work):

```bash
indemn actor transition <associate-id> --to suspended
```

---

## 7. Testing

### Verify the associate claims messages

1. Trigger an event that matches the associate's role watches (e.g., create an email entity).
2. Check the queue:
   ```bash
   indemn queue stats --role email_classifier
   ```
3. The message count should decrease as the associate claims it.

### Verify entity changes

After the associate processes a message, check that the entity was updated correctly:

```bash
indemn entity get <entity-id>
```

### Trace the full cascade

See every action the associate took in response to a message:

```bash
indemn trace cascade <message-id>
```

This shows: message claimed, entity reads, entity writes, any downstream events triggered.

### End-to-end test

```bash
# Create a test email
indemn email create --data '{
  "sender": "john@travelers.com",
  "subject": "RE: Policy Renewal GLB-2024-001",
  "body": "Please find attached the renewal terms..."
}'

# Wait a few seconds, then check
indemn entity get <email-id>
# Should show classification: "carrier_response"
```

---

## 8. Adding Rules to Reduce AI Cost

The `--auto` pattern is the primary mechanism for cost optimization. When an associate runs in `hybrid` mode:

1. The `--auto` command checks deterministic rules first
2. If rules match, the action is applied with zero AI cost
3. If no rules match, `--auto` returns `needs_reasoning` and the LLM handles it

**Adding rules over time:**

Watch what the associate classifies using reasoning. When you see repeated patterns, codify them as rules:

```bash
# Add a rule: emails from @nationwide.com are always carrier_response
indemn rule create --data '{
  "entity_type": "Email",
  "capability": "auto_classify",
  "name": "nationwide-domain",
  "conditions": {"field": "sender_domain", "op": "equals", "value": "nationwide.com"},
  "action": "set_fields",
  "sets": {"classification": "carrier_response"},
  "priority": 200
}'
```

Over time, the `needs_reasoning` rate drops as rules cover more cases. Track this:

```bash
indemn actor stats <associate-id> --period 7d
```

Look at the `reasoning_rate` metric. A well-tuned hybrid associate should handle 70-90% of messages deterministically.

---

## 9. Monitoring

### Queue health

```bash
# Messages waiting, in-progress, completed
indemn queue stats --role email_classifier

# Queue depth over time (check for growing backlog)
indemn queue stats --role email_classifier --period 24h
```

### Associate performance

```bash
# Processing time, success rate, reasoning rate
indemn actor stats <associate-id>

# Detailed trace for a specific message
indemn trace cascade <message-id>
```

### Cost tracking

```bash
# AI cost breakdown per associate
indemn actor stats <associate-id> --cost

# Across all associates
indemn actor stats --all --cost --period 7d
```

### Alerts to watch for

- **Growing queue depth**: Associate is not keeping up. Check if it is active, if the runtime is responding, or if processing time has increased.
- **High reasoning rate**: Rules are not covering enough cases. Review recent `needs_reasoning` messages and add rules.
- **Escalation spikes**: Associate is hitting ambiguous cases more than expected. Review the skill instructions for clarity.

---

## 10. The Gradual Rollout

Do not deploy an associate and walk away. Follow this progression:

### Phase 1: Human only
A human handles all work. Log what they do to identify patterns.

### Phase 2: Human + associate (shadow mode)
The associate processes messages but does not write to entities. Compare its decisions against the human's. Fix the skill and rules based on discrepancies.

```bash
indemn actor create --type associate --name "Email Classifier" \
  --mode hybrid --runtime-id <runtime-id> --role email_classifier \
  --skills email-classifier --shadow
```

### Phase 3: Associate with escalation
The associate processes messages and writes to entities. Ambiguous cases go to a human. Monitor closely for the first week.

```bash
indemn actor transition <associate-id> --to active
# Remove --shadow flag by updating the actor
indemn actor update <associate-id> --shadow false
```

### Phase 4: Associate primary, human backup
The associate handles the majority of work. Humans handle only escalated cases and periodic audits.

At each phase, review:
- Accuracy of decisions
- Processing time
- Escalation rate
- Cost per message
- Edge cases the skill does not cover

Only advance to the next phase when metrics are stable for at least one week.
