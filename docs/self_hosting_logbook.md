# Self-hosted Crucible: logbook

A plain-language record of the work to make Crucible something any research group can install on their own computers. Newest entries at the bottom. Technical details are in [the design](self_hosting_design.md) and [the Phase 1 plan](self_hosting_phase1_plan.md).

## The goal in one paragraph

Today Crucible only runs in one place: the Molecular Foundry's setup on Google Cloud. We want a small lab with a couple of workstations to be able to run its own Crucible (its own database, its own file storage, its own website) without a Google account and without paid software. Bigger institutions can run the same thing on a public web address, as the Foundry does. The Foundry keeps running throughout and should become easier to maintain, not harder.

## The phases

1. **A fresh install works.** Make it possible to start a brand-new Crucible from scratch with one command on one computer. Today that is impossible: the software quietly assumes the Foundry's Google setup in many places, and there is no way to create an empty database with the right structure.
2. **Our own file storage.** Change how Crucible stores files so it can use any storage system that speaks the common "S3" language, including free software a lab can run itself. The Foundry keeps its files on Google Cloud, reached the same way. The old Google-only upload methods are retired.
3. **Simple passwords for small labs.** Add username-and-password sign-in for small installations of a few users, built to current security standards. ORCID sign-in stays and is recommended for larger installations.
4. **Releases.** Give the software an open-source license (after review by Berkeley Lab's Intellectual Property Office), make the code public, and publish ready-to-run packages with version numbers and upgrade instructions. Move the Foundry's own server configuration into a separate private repository so it is not published.
5. **Optional add-ons.** Automatic processing of uploaded files (ingestion), label printing, and instrument sensor data, each something a lab can switch on if it wants.
6. **Later.** Running heavy processing on computing clusters, and moving large data with Globus.

## Entries

### 2026-10-21: Where we are and where we want to go

- Mapped every place where Crucible depends on Google Cloud or on Foundry-specific services, across the main software, the processing services, the website (the Hub), and the Python client.
- Main findings: the database cannot be created from scratch today; file storage is tied to Google in many places; many Foundry addresses and passwords are built into the code as hidden defaults; the Python client and the Hub mostly just need to be pointed at a different address.
- Decided the scope: the base installation is the database, the main software (the API), the website (the Hub), and file storage. The graph explorer is retired. Printing and automatic processing become optional add-ons.

### 2026-10-22: Design agreed

- Wrote and agreed the design. Key decisions:
  - Two kinds of installation: a lab on its local network, and an institution on a public web address. Same software, different settings.
  - File storage uses the common "S3" language everywhere. The Foundry keeps its files where they are.
  - Sign-in: ORCID stays; simple passwords for small labs. We considered running a full identity system (such as Keycloak) and decided it is too much for now.
  - Password handling follows current official guidance (NIST and OWASP): long passwords allowed, no forced periodic changes, checks against known leaked passwords, and protection against repeated guessing.
- Set up a separate working copy of the code so this project does not interfere with the day-to-day work on the API.

### 2026-10-22: Phase 1 planned and reviewed

- Wrote a step-by-step plan for Phase 1 (ten tasks).
- Had eight independent AI reviewers (Google Gemini) check the plan against the code. They found twenty real problems the plan had missed, mostly places where the software reads settings too early and would crash once the Foundry defaults are removed. All twenty are now handled in the plan. Some of their claims were wrong and were discarded after checking.
- Agreed a strict order to protect the Foundry: the first task only writes down the Foundry's current settings in its configuration files, and nothing else is changed until that is live in production.
- Agreed how settings and passwords should be handled long term, following common industry practice: the software should never go looking for its own settings or passwords; whoever runs it hands them over at startup. We get there in two steps: first make everything explicit (Phase 1), later switch the Foundry so its servers hand the passwords to the software (a separate, careful change).

### 2026-10-23: Tasks 1 and 2 on staging

- Task 1 writes the Foundry's current hidden settings (addresses, ports, which features are on, and where each password is kept in Google) into the Foundry's configuration files. The software itself does not change, so the Foundry behaves exactly as before.
- Checked first that none of these settings already existed on the live servers. Added an automatic test that checks every value. Three independent AI reviewers found no problems.
- Opened the change for staging (pull request #358). Production follows only after staging is verified, and only when the project lead releases it.
- Decided where configuration lives: the Foundry's settings stay in git (reviewed and versioned), but move to a separate private repository before the code is made public.
- Found that making a repository public also publishes its full history, which includes Foundry infrastructure details and some password files that were deleted long ago. Plan: scan the history for passwords and replace any that are found, then publish from a fresh, clean copy of the code while keeping the old repository private.
- Decided how the Foundry will deploy in future (Phase 4): the software gets numbered versions, and the Foundry's private configuration says which version each server runs. Updating, promoting to production, or going back is then a one-line, reviewed change, instead of every code merge going straight to the servers as today.
- The change went live on staging. Checked on the live staging server that all eighteen settings arrived with the right values, that the hourly clean-up task received what it needs and is still switched on, and that the service reports itself healthy. The full automated test suite, run with three versions of the Python client, passed against the updated staging server.
- Decided that production changes only once, at the end of Phase 1, after every step works on staging. Checked that this is safe: when the release goes out, each new server copy starts with the new settings and the new code together, and copies that restart in the meantime keep the old version.
- Task 2 written and reviewed: the software now only takes passwords from where it is explicitly told (a setting, a file, or a named entry in Google's password store), and stops at startup with a clear message if one is missing. It no longer guesses the Foundry's Google addresses. The Foundry's servers read the same passwords from the same places as before. Three AI reviewers found only small edge cases (for example an empty password file being accepted), all fixed. Opened for staging as pull request #359.
- Task 2 went live on staging and was checked: the servers start, all seven passwords are found where the configuration says, and the service is healthy.
- Switched the way settings and passwords are read to the standard library recommended for this kind of software (pydantic-settings), replacing our own small helper. Passwords can now also be given as files in a standard folder, which is how Docker hands them over, and they are hidden from logs. Three AI reviewers found only small edge cases, all fixed. Opened for staging as pull request #360.
- Simplified the configuration code further: everything about settings and passwords now lives in one file, using the library's own features instead of extra custom code.

### 2026-10-26: Settings change live on staging

- The new way of reading settings went live on staging and was checked: the server starts, all seven passwords are found, the service is healthy, and the hourly clean-up task ran successfully on the new version.
- The automated test suite passed against staging with the new settings code.
- Task 3 written and reviewed: printing, automatic file processing, and the Foundry proposal lookup can now be switched on or off (off for new installations, on for the Foundry). When a feature is off, the software says so clearly instead of trying to reach a server that is not there. All remaining Foundry addresses were removed from the code. If file storage is not set up, the software says so instead of crashing. The server now checks its settings when it starts and refuses to start with a clear list if something required is missing. Three AI reviewers found only small issues in old helper scripts and edge cases, all fixed. Opened for staging as pull request #362.
