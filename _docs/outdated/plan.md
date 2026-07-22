# Weekly Team Feedback Tool — MVP Scope

## Product goal

Help project teams collect honest weekly feedback, turn it into a focused retrospective, and leave the meeting with documented decisions and action items.

## Core workflow

### 1. Create a feedback cycle

A facilitator creates a weekly feedback cycle for a project and invites the team.

The facilitator is usually the project owner, but the role can be assigned to another team member.

### 2. Collect feedback

Every team member submits feedback using:

- **Start:** What should the team begin doing?
- **Stop:** What should the team stop doing?
- **Continue:** What is working and should continue?

Submissions are attributed by default. Contributors can select **Submit anonymously** for individual entries.

Before the retrospective, contributors can see and edit only their own feedback. They cannot see other team members’ submissions.

### 3. Reveal and cluster feedback

The facilitator starts the retrospective and reveals all submissions at once.

The tool suggests thematic clusters automatically. The team can then:

- Move cards between clusters
- Merge or split clusters
- Rename clusters
- Leave cards ungrouped

### 4. Vote on discussion topics

Each team member receives **3 votes**.

They may place multiple or all votes on the same cluster.

Clusters are ranked by total votes, producing a prioritized discussion agenda.

### 5. Run the discussion

The facilitator works through the prioritized topics and marks each one as:

- Discussed
- Skipped
- Deferred

Team members can manually record notes, decisions, and action items during the meeting.

### 6. Process the meeting record

After the meeting, the facilitator can upload:

- Audio
- Video
- A transcript file
- Pasted transcript text

The system generates a transcript when necessary and suggests:

- Decisions made
- Action items
- Action owners
- Due dates, when mentioned
- A short retrospective summary

The facilitator reviews and confirms these suggestions before they are saved.

## Decisions made for the MVP

### Extracted results require facilitator approval

**Decision:** AI-generated actions and decisions remain drafts until the facilitator confirms them.

**Why:** Transcription and extraction can misunderstand context, ownership, or tentative statements. Automatic publishing would reduce trust and could assign work incorrectly.

**Alternatives considered:**

- Automatic saving: faster, but too risky.
- Entire-team approval: safer, but creates unnecessary friction.
- Action-owner approval: useful later, but adds notifications and workflow complexity.

### Feedback is submitted as separate cards

**Decision:** Team members can create multiple short cards under Start, Stop, and Continue.

**Why:** Separate cards are easier to cluster, vote on, move, and discuss than one large response.

**Alternative considered:** One text field per category. Simpler to build, but harder to organize during the retrospective.

### Anonymous feedback stays anonymous

**Decision:** The system does not reveal anonymous authors to the facilitator or team.

**Why:** “Anonymous” should have a clear and trustworthy meaning. Hidden administrator access would discourage honest feedback.

**Alternative considered:** Anonymous to teammates but visible to facilitators. This may be useful in some organizations, but it weakens psychological safety.

### Voting is visible after voting closes

**Decision:** Participants do not see live vote totals while voting. Results appear when everyone has voted or the facilitator closes voting.

**Why:** Hidden totals reduce group influence and popularity bias.

**Alternatives considered:**

- Live totals: more engaging, but encourages people to follow existing votes.
- Permanently private votes: less transparent and harder to facilitate.

### Automatic clustering is always editable

**Decision:** AI proposes clusters but never finalizes them.

**Why:** Similar wording does not always mean the same underlying problem. The team understands its context better than the model.

**Alternative considered:** Fully automatic clustering. Faster, but likely to create confusing or incorrect groupings.

### Action items have a simple structure

Each action contains:

- Description
- Owner
- Optional due date
- Status: Open or Done
- Related discussion topic

**Why:** This is enough to make outcomes accountable without turning the MVP into a project-management platform.

**Alternatives considered:** Priorities, subtasks, dependencies, reminders, and recurring tasks. These should be deferred or handled through later integrations.

### One retrospective belongs to one project

**Decision:** Feedback cycles and retrospectives are organized within projects.

**Why:** It provides enough structure for recurring teams while keeping permissions and history understandable.

**Alternative considered:** Organization-wide retrospectives without projects. Simpler initially, but becomes confusing once users participate in multiple teams.

## Main screens

### Project page

Shows:

- Current feedback cycle
- Submission status
- Upcoming or active retrospective
- Previous retrospectives
- Open action items

### Feedback form

Shows three columns or sections:

- Start
- Stop
- Continue

Each entry includes an anonymous checkbox.

### Retrospective board

Supports four modes:

1. Reveal
2. Cluster
3. Vote
4. Discuss

### Meeting upload page

Allows audio, video, transcript-file upload, or pasted text.

Shows processing status and generated results.

### Retrospective summary

Contains:

- Top discussion topics
- Key notes
- Confirmed decisions
- Confirmed action items
- Attendance and participation
- Original feedback cards

## Roles

### Team member

Can:

- Submit and edit their own feedback
- Choose attribution or anonymity
- Participate in clustering
- Vote
- View completed retrospective summaries
- Update actions assigned to them

### Facilitator

Can also:

- Create and close feedback cycles
- Start the retrospective
- Reveal feedback
- Control the retrospective stages
- Upload meeting records
- Review extracted outcomes
- Edit and publish the final summary

## Explicitly excluded from the MVP

- Built-in meeting recording
- Zoom, Google Meet, or Microsoft Teams integrations
- Slack or email integrations
- Advanced project-management features
- Automated reminders and escalation
- Sentiment or employee-performance scoring
- Cross-project analytics
- Custom retrospective frameworks
- Real-time collaborative transcript editing
- Multiple approval workflows

## Suggested success metrics

- Percentage of invited members who submit feedback
- Percentage of retrospectives completed
- Number of confirmed actions per retrospective
- Percentage of actions completed before the next retrospective
- Time from meeting upload to published summary
- Repeat weekly usage by teams

## MVP definition

The MVP is successful when a team can:

1. Create a project and weekly feedback cycle
2. Collect private Start, Stop, and Continue cards
3. Reveal and collaboratively cluster the cards
4. Vote with three stackable votes per person
5. Discuss topics in priority order
6. Upload a meeting recording or transcript
7. Review extracted actions and decisions
8. Publish a retrospective summary

## Key scope principle

Keep this a **retrospective workflow**, not a meeting recorder, survey platform, or project-management system.
