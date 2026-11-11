export function initGoogleHome({ $, api, announce, copyText, loadSpeakers }) {
  let ghWizardOpen = false;
  const GH_COMPANION = 'http://127.0.0.1:18765';
  let ghPairingToken = sessionStorage.getItem('ghPairingToken') || '';
  const GH_STEPS = [
    { id: 'welcome', short: 'Welcome', title: 'Welcome', body: 'Confirm Plex and Cast are ready, then verify this checklist.', action: 'verify', actionLabel: 'Check prerequisites' },
    { id: 'cloud_auth', short: 'Cloud', title: 'Connect Google Cloud', body: 'Create a new GCP project or attach an existing one (for example plex-cast-bridge), then verify.', action: 'create_project', actionLabel: 'Create project on this Mac' },
    { id: 'prepare_services', short: 'Services', title: 'Prepare cloud services', body: 'Link billing for this project, then provision APIs and resources on this Mac.', action: 'provision', actionLabel: 'Run provision on this Mac' },
    { id: 'subscriber_auth', short: 'Secrets', title: 'Local subscriber auth', body: 'Mint secrets on this Mac and keep the catalog write key on the bridge.', action: 'mint', actionLabel: 'Mint secrets on this Mac' },
    { id: 'deploy_gateway', short: 'Deploy', title: 'Deploy cloud gateway', body: 'Deploy the Cloud Run gateway from this Mac (or paste an existing HTTPS URL), then verify health.', action: 'deploy', actionLabel: 'Deploy Cloud Run' },
    { id: 'developer_console', short: 'Console', title: 'Developer Console', body: 'Paste OAuth values into Google Home Developer Console, then verify.', action: 'verify', actionLabel: 'Verify console setup' },
    { id: 'link_home', short: 'Link', title: 'Link Google Home', body: 'In the Google Home app, link [test] Plex Cast Bridge, approve the matching code in the dashboard, then assign rooms.', action: 'verify', actionLabel: 'Verify Google Home link' },
    { id: 'connection_test', short: 'Test', title: 'Connection test', body: 'Run end-to-end diagnostics. Finish when critical checks pass.', action: 'test', actionLabel: 'Run connection test' },
  ];
  let ghStepIndex = 0;
  let ghSetupCache = {};
  let ghStepVerified = false;
  let ghCompanionAuth = null;
  let ghBusy = false;
  let ghDoMode = 'action';

  async function companionFetch(path, options = {}) {
    const res = await fetch(`${GH_COMPANION}${path}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(ghPairingToken ? { 'X-Companion-Token': ghPairingToken } : {}),
        ...(options.headers || {}),
      },
    });
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
    if (!res.ok) throw new Error(data.error || data.detail || `Companion HTTP ${res.status}`);
    return data;
  }

  function bindGhPairingInput() {
    const input = $('ghPairingTokenInput');
    if (!input || input.dataset.bound === '1') return;
    input.dataset.bound = '1';
    if (ghPairingToken) input.value = ghPairingToken;
    input.addEventListener('change', async () => {
      ghPairingToken = input.value.trim();
      if (ghPairingToken) sessionStorage.setItem('ghPairingToken', ghPairingToken);
      else sessionStorage.removeItem('ghPairingToken');
      await refreshCompanionStatus();
    });
    input.addEventListener('keydown', async (ev) => {
      if (ev.key !== 'Enter') return;
      ev.preventDefault();
      input.dispatchEvent(new Event('change'));
    });
  }

  async function refreshCompanionStatus() {
    const el = $('ghCompanionStatus');
    try {
      await companionFetch('/health');
      ghCompanionAuth = await companionFetch('/auth/status');
      el.classList.add('is-ok');
      el.textContent = ghCompanionAuth.ok
        ? `Companion ready · ${ghCompanionAuth.account || 'gcloud'} · ADC ok`
        : `Companion up · gcloud needs login: ${ghCompanionAuth.detail || 'auth incomplete'}`;
      return true;
    } catch (_) {
      ghCompanionAuth = null;
      el.classList.remove('is-ok');
      el.textContent = 'Companion offline — on this Mac run: uv run python scripts/gh_setup_companion.py serve';
      return false;
    }
  }

  function fieldInput(id, label, placeholder) {
    return `<label class="field-label" for="${id}">${label}</label>
      <div class="field-wrap"><input id="${id}" class="field-select" placeholder="${placeholder || ''}" autocomplete="off"></div>`;
  }

  function setGhBusy(on, label) {
    ghBusy = Boolean(on);
    const doBtn = $('ghDoStepBtn');
    const nextBtn = $('ghNextBtn');
    const backBtn = $('ghBackBtn');
    doBtn.classList.toggle('is-busy', ghBusy);
    nextBtn.classList.toggle('is-busy', ghBusy);
    backBtn.disabled = ghBusy;
    if (ghBusy) {
      doBtn.disabled = true;
      nextBtn.disabled = true;
      doBtn.innerHTML = `<span class="gh-busy" aria-hidden="true"></span> ${label || 'Working…'}`;
    } else {
      updateGhActionButtons();
    }
  }

  function renderGhStepper() {
    const nav = $('ghStepper');
    nav.replaceChildren();
    const completed = new Set(ghSetupCache.completed_steps || []);
    const verified = ghSetupCache.verified || {};
    GH_STEPS.forEach((step, idx) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'gh-step';
      if (idx === ghStepIndex) btn.classList.add('is-current');
      if (completed.has(step.id) || verified[step.id]) btn.classList.add('is-done');
      const canJump = idx <= ghStepIndex || completed.has(GH_STEPS[idx - 1]?.id) || verified[GH_STEPS[idx - 1]?.id];
      if (canJump && idx !== ghStepIndex) btn.classList.add('is-clickable');
      btn.innerHTML = `<span class="n">${completed.has(step.id) || verified[step.id] ? '✓' : idx + 1}</span><span class="t">${step.short}</span>`;
      btn.addEventListener('click', () => {
        if (!canJump || ghBusy) return;
        ghStepIndex = idx;
        renderGhWizardStep();
      });
      nav.appendChild(btn);
    });
  }

  function renderGhFields() {
    const step = GH_STEPS[ghStepIndex];
    const box = $('ghWizardFields');
    const s = ghSetupCache || {};
    const progress = s.provision_progress || {};
    if (step.id === 'welcome') {
      box.innerHTML = `<p class="conn-meta">Uses the Plex card above and speakers from Player. This checklist is only for Google Home setup readiness.</p>${s.installation_detected ? `<p class="conn-meta">Existing Google Home gateway configuration detected (${s.recommended_operation || 'recovery'}). This installation will not create a project or mint credentials.</p>` : ''}`;
    } else if (step.id === 'cloud_auth') {
      if (s.installation_detected) {
        box.innerHTML = `<p class="conn-meta">An existing Google Home gateway is configured. First-time provisioning and credential generation are disabled. Use Connection test for recovery checks, or the image-only upgrade procedure for code updates.</p>`;
        return;
      }
      const mode = s.mode === 'attach_existing' ? 'attach_existing' : 'create_new_project';
      box.innerHTML = `
        <fieldset class="gh-field-grid" style="border:0;padding:0;margin:0">
          <legend class="field-label">Project source</legend>
          <label class="gh-checkrow"><input type="radio" name="ghProjectMode" value="create_new_project" ${mode !== 'attach_existing' ? 'checked' : ''}> Create a new GCP project</label>
          <label class="gh-checkrow"><input type="radio" name="ghProjectMode" value="attach_existing" ${mode === 'attach_existing' ? 'checked' : ''}> Use an existing GCP project</label>
        </fieldset>
        ${fieldInput('ghProjectId', mode === 'attach_existing' ? 'Existing GCP project ID' : 'New GCP project ID', mode === 'attach_existing' ? 'e.g. plex-cast-bridge' : 'e.g. plex-cast-home-a7f3')}
        <p class="conn-meta" id="ghProjectModeHint">${mode === 'attach_existing'
          ? 'We verify the project is ACTIVE with your local gcloud, then continue inside it.'
          : 'Deleted project IDs cannot be reused. Prefer a fresh ID only when you need a brand-new project.'}</p>`;
      $('ghProjectId').value = s.project_id || (mode === 'attach_existing' ? 'plex-cast-bridge' : '');
      box.querySelectorAll('input[name="ghProjectMode"]').forEach((el) => {
        el.addEventListener('change', () => {
          const next = box.querySelector('input[name="ghProjectMode"]:checked')?.value || 'create_new_project';
          const typed = $('ghProjectId')?.value?.trim() || '';
          ghSetupCache = {
            ...ghSetupCache,
            mode: next,
            project_id: typed || (next === 'attach_existing' ? 'plex-cast-bridge' : ghSetupCache.project_id),
          };
          renderGhFields();
          updateGhActionButtons();
        });
      });
    } else if (step.id === 'prepare_services') {
      const billingUrl = `https://console.cloud.google.com/billing/linkedaccount?project=${encodeURIComponent(s.project_id || '')}`;
      const billingOk = Boolean(s.billing_confirmed) || progress.confirm_billing === 'ok';
      box.innerHTML = `
        <p class="conn-meta">Project: <code id="ghProjectSummary"></code></p>
        <p class="gh-status-pill ${billingOk ? 'is-ok' : 'is-bad'}" id="ghBillingStatus">
          <span class="dot" aria-hidden="true"></span>
          <span>${billingOk ? 'Billing linked' : 'Billing not linked yet'}</span>
        </p>
        <p class="conn-meta">Google requires a billing account for Cloud Run / Pub/Sub even on personal free-tier usage. <a href="${billingUrl}" target="_blank" rel="noopener">Open billing console</a> or use Do this step to link your open Default Billing Account, then provision.</p>
        <p class="conn-meta" id="ghProvisionNote"></p>`;
      $('ghProjectSummary').textContent = s.project_id || '(set in previous step)';
      $('ghProvisionNote').textContent = `Progress: ${Object.keys(progress).length ? JSON.stringify(progress) : 'not started'}`;
    } else if (step.id === 'subscriber_auth') {
      box.innerHTML = `
        ${fieldInput('ghCatalogWriteKey', 'CATALOG_WRITE_KEY (auto-filled after mint)', 'paste from mint output', '')}
        <div class="gh-icon-card" id="ghSecretsPassphraseCard" hidden>
          <div class="meta" style="width:100%">
            <strong>Admin passphrase (for Google Home linking)</strong>
            <span>You’ll enter this on the OAuth login page when linking in the Google Home app — not your Google password.</span>
            <ul class="gh-paste-list" id="ghSecretsPasteList"></ul>
          </div>
        </div>
        <p class="conn-meta">Mint writes secrets to <code>gcp-cloud-run/.env</code> on this Mac. Only the catalog write key is stored on the bridge.</p>`;
    } else if (step.id === 'deploy_gateway') {
      box.innerHTML = `
        ${fieldInput('ghCloudRunUrl', 'Cloud Run service URL', 'https://….run.app')}
        <p class="conn-meta">Click <strong>Deploy Cloud Run</strong> on this Mac (takes a few minutes). The URL fills in automatically. Or paste an existing <code>*.run.app</code> URL, then Check this step.</p>`;
      $('ghCloudRunUrl').value = s.cloud_run_url || '';
    } else if (step.id === 'developer_console') {
      const oauth = s.oauth || {};
      box.innerHTML = `
        <p class="conn-meta">Google’s Developer Console can’t be prefilled from apps — copy each value below into it. This Home Console project is <em>separate</em> from your GCP project.</p>
        <div class="conn-actions">
          <button type="button" class="ghost-button" id="ghLoadPastePack">Load copy pack from this Mac</button>
          <a class="ghost-button" href="https://console.home.google.com/projects" target="_blank" rel="noopener">Open Developer Console</a>
        </div>
        <div class="gh-icon-card">
          <img src="/assets/icon-144.png" alt="Plex Cast Bridge 144px icon" width="72" height="72" />
          <div class="meta">
            <strong>App branding icon (144×144 PNG)</strong>
            <span>Download and upload this in Developer Console → App branding. Device type: <strong>TV</strong> or <strong>Audio Video Receiver</strong>.</span>
            <div class="conn-actions">
              <a class="ghost-button" href="/assets/icon-144.png" download="plex-cast-bridge-icon-144.png">Download icon</a>
            </div>
          </div>
        </div>
        <ol class="gh-howto" id="ghConsoleHowto">
          <li>Open your Home Console project → Add <strong>Cloud-to-cloud</strong> → Setup &amp; configuration</li>
          <li>Name: Plex Cast Bridge · Device type: <strong>TV</strong> or <strong>Audio Video Receiver</strong></li>
          <li>Upload the 144×144 icon from this step · leave <strong>Local fulfillment</strong> and <strong>App Flip</strong> off</li>
          <li>Paste Client ID / secret / Authorization / Token / Fulfillment URLs</li>
          <li>Save in Google’s console, then click <strong>Save &amp; verify console</strong> here</li>
        </ol>
        <ul class="gh-paste-list" id="ghPasteList"></ul>
        ${fieldInput('ghClientId', 'OAuth client ID (confirm)', 'from mint')}`;
      $('ghClientId').value = oauth.client_id || '';
      const renderPaste = (vals) => {
        const rows = [
          ['Integration name', vals.integration_name],
          ['Device type', vals.device_type || 'TV or Audio Video Receiver'],
          ['Client ID', vals.client_id],
          ['Client secret', vals.client_secret],
          ['Admin passphrase', vals.admin_passphrase],
          ['Authorization URL', vals.authorization_url],
          ['Token URL', vals.token_url],
          ['Fulfillment URL', vals.fulfillment_url],
        ].filter(([, v]) => v);
        const ul = $('ghPasteList');
        ul.replaceChildren();
        rows.forEach(([label, value]) => {
          const li = document.createElement('li');
          li.innerHTML = `<span class="label"></span><code></code><button type="button" class="ghost-button">Copy</button>`;
          li.querySelector('.label').textContent = label;
          li.querySelector('code').textContent = value;
          li.querySelector('code').title = value;
          li.querySelector('button').addEventListener('click', async () => {
            try {
              await copyText(value);
              announce(`Copied ${label}`);
            } catch (_) {
              announce('Copy failed — select the value manually');
            }
          });
          ul.appendChild(li);
        });
        if (vals.client_id && $('ghClientId')) $('ghClientId').value = vals.client_id;
        if (Array.isArray(vals.steps) && vals.steps.length) {
          const ol = $('ghConsoleHowto');
          ol.replaceChildren();
          vals.steps.forEach((t) => {
            const li = document.createElement('li');
            li.textContent = t;
            ol.appendChild(li);
          });
        }
        $('ghConsoleValues').textContent = JSON.stringify({
          ...vals,
          client_secret: vals.client_secret ? '[copied via Copy button — not logged]' : null,
        }, null, 2);
      };
      // Prefill URLs from wizard state immediately; secret comes from companion.
      renderPaste({
        integration_name: 'Plex Cast Bridge',
        device_type: 'TV or Audio Video Receiver',
        client_id: oauth.client_id,
        authorization_url: oauth.authorization_url,
        token_url: oauth.token_url,
        fulfillment_url: oauth.fulfillment_url,
      });
      $('ghLoadPastePack')?.addEventListener('click', async () => {
        try {
          await refreshCompanionStatus();
          const vals = await companionFetch('/console-paste');
          renderPaste(vals);
          announce(vals.client_secret ? 'Paste pack ready — use Copy on each row' : 'Loaded URLs; client secret missing in gcp-cloud-run/.env');
        } catch (err) {
          announce(err.message || 'Could not load paste pack (is companion running?)');
        }
      });
    } else if (step.id === 'link_home') {
      box.innerHTML = `
        <p class="conn-meta">Approve linking in the local dashboard. Use <strong>Back</strong> anytime to revisit earlier steps.</p>
        <ol class="gh-howto" id="ghLinkHowto">
          <li>On your phone, open the <strong>Google Home</strong> app (same Google account as Developer Console)</li>
          <li><strong>Devices</strong> → <strong>Add / +</strong> → <strong>Works with Google</strong></li>
          <li>Search for <strong>[test] Plex Cast Bridge</strong> and tap it</li>
          <li>Allow the browser/login sheet — it should finish automatically (no passphrase)</li>
          <li>Assign each new device to a room in Google Home</li>
          <li>Return here and click <strong>Confirm linked</strong></li>
        </ol>
        <details class="advanced-block" style="margin-top:12px">
          <summary>Optional: passphrase mode / copy admin passphrase</summary>
          <p class="conn-meta">Use the matching code shown during Google Home linking.</p>
          <ul class="gh-paste-list" id="ghLinkPasteList"></ul>
          <div class="conn-actions" style="margin-top:8px">
            <button type="button" class="ghost-button" id="ghLoadLinkSecrets">Load passphrase from this Mac</button>
          </div>
        </details>`;
      const renderLinkPaste = (vals) => {
        const ul = $('ghLinkPasteList');
        ul.replaceChildren();
        const rows = [['Admin passphrase', vals.admin_passphrase]].filter(([, v]) => v);
        if (!rows.length) {
          const li = document.createElement('li');
          li.innerHTML = `<span class="label">Admin passphrase</span><code>Not found — click Load</code>`;
          ul.appendChild(li);
          return;
        }
        rows.forEach(([label, value]) => {
          const li = document.createElement('li');
          li.innerHTML = `<span class="label"></span><code></code><button type="button" class="ghost-button">Copy</button>`;
          li.querySelector('.label').textContent = label;
          li.querySelector('code').textContent = value;
          li.querySelector('code').title = value;
          li.querySelector('button').addEventListener('click', async () => {
            try { await copyText(value); announce(`Copied ${label}`); }
            catch (_) { announce('Copy failed — select the value manually'); }
          });
          ul.appendChild(li);
        });
        if (Array.isArray(vals.link_steps) && vals.link_steps.length) {
          const ol = $('ghLinkHowto');
          // Keep the no-passphrase howto; don't overwrite with old passphrase steps.
        }
      };
      $('ghLoadLinkSecrets')?.addEventListener('click', async () => {
        try {
          await refreshCompanionStatus();
          const vals = await companionFetch('/console-paste');
          renderLinkPaste(vals);
          announce(vals.admin_passphrase ? 'Passphrase loaded' : 'Admin passphrase missing in gcp-cloud-run/.env');
        } catch (err) {
          announce(err.message || 'Could not load passphrase (is companion running?)');
        }
      });
    } else if (step.id === 'connection_test') {
      const summary = document.createElement('p');
      summary.className = 'conn-meta';
      summary.textContent = s.last_test?.summary || 'No test run yet. Click Do this step to run diagnostics.';
      box.replaceChildren(summary);
    } else {
      box.replaceChildren();
    }
  }

  function renderGhChecks(checks) {
    const wrap = $('ghChecksWrap');
    const ul = $('ghVerifyChecks');
    ul.replaceChildren();
    if (!checks || !checks.length) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    $('ghChecksTitle').textContent = `Checks for ${GH_STEPS[ghStepIndex].title}`;
    checks.forEach((c) => {
      const li = document.createElement('li');
      li.className = c.ok ? 'ok' : 'bad';
      const mark = document.createElement('span');
      mark.className = 'mark';
      mark.setAttribute('aria-hidden', 'true');
      mark.textContent = c.ok ? '✓' : '!';
      const details = document.createElement('span');
      const name = document.createElement('strong');
      name.textContent = c.name || '';
      details.append(name, document.createTextNode(` — ${c.detail || ''}`));
      if (c.recovery && !c.ok) {
        const recovery = document.createElement('small');
        recovery.textContent = c.recovery;
        details.appendChild(recovery);
      }
      li.append(mark, details);
      ul.appendChild(li);
    });
  }

  function updateGhActionButtons() {
    const step = GH_STEPS[ghStepIndex];
    const doBtn = $('ghDoStepBtn');
    const nextBtn = $('ghNextBtn');
    const backBtn = $('ghBackBtn');
    const blocked = $('ghNextBlocked');
    const blockedText = $('ghNextBlockedText');
    backBtn.disabled = ghBusy || ghStepIndex <= 0;
    doBtn.disabled = ghBusy;

    const cloudMode = (document.querySelector('input[name="ghProjectMode"]:checked')?.value)
      || ghSetupCache.mode
      || 'create_new_project';
    const cloudActionLabel = cloudMode === 'attach_existing'
      ? 'Use existing project'
      : 'Create project';
    const progress = ghSetupCache.provision_progress || {};
    const billingOk = Boolean(ghSetupCache.billing_confirmed) || progress.confirm_billing === 'ok';
    const billingNeedsLink = step.id === 'prepare_services' && !billingOk;

    if (ghSetupCache.installation_detected && ['cloud_auth', 'prepare_services', 'subscriber_auth', 'deploy_gateway'].includes(step.id)) {
      doBtn.disabled = true;
      doBtn.textContent = 'Existing installation — provisioning disabled';
      return;
    }

    if (step.id === 'cloud_auth') {
      ghDoMode = 'create_or_attach';
      doBtn.textContent = cloudActionLabel;
    } else if (step.id === 'prepare_services') {
      if (billingNeedsLink) {
        ghDoMode = 'check_billing';
        doBtn.textContent = 'Refresh billing status';
      } else {
        ghDoMode = 'provision';
        doBtn.textContent = 'Provision services';
      }
    } else if (step.id === 'subscriber_auth') {
      ghDoMode = 'mint';
      doBtn.textContent = 'Mint secrets';
    } else if (step.id === 'deploy_gateway') {
      const hasUrl = Boolean(($('ghCloudRunUrl')?.value || s.cloud_run_url || '').trim());
      if (hasUrl && !ghStepVerified) {
        ghDoMode = 'verify';
        doBtn.textContent = 'Check this step';
      } else {
        ghDoMode = 'deploy';
        doBtn.textContent = hasUrl ? 'Redeploy Cloud Run' : 'Deploy Cloud Run';
      }
    } else if (step.id === 'developer_console') {
      ghDoMode = 'mark_console';
      doBtn.textContent = 'Save & verify console';
    } else if (step.id === 'link_home') {
      ghDoMode = 'confirm_link';
      doBtn.textContent = 'Confirm linked';
    } else if (step.id === 'connection_test') {
      ghDoMode = 'test';
      doBtn.textContent = 'Run connection test';
    } else {
      ghDoMode = 'verify';
      doBtn.textContent = 'Check this step';
    }

    if (ghStepVerified) {
      nextBtn.disabled = ghBusy;
      nextBtn.classList.remove('is-blocked-next');
      nextBtn.textContent = ghStepIndex >= GH_STEPS.length - 1 ? 'Finish' : 'Next';
      blocked.classList.remove('is-visible');
      $('ghNextHint').className = 'gh-next-hint is-ok';
      $('ghNextHint').textContent = 'Step complete. Click Next to continue.';
      return;
    }

    nextBtn.disabled = true;
    nextBtn.classList.add('is-blocked-next');
    nextBtn.textContent = 'Next';
    blocked.classList.add('is-visible');
    if (billingNeedsLink) {
      blockedText.textContent = 'Next locked — billing not linked';
      $('ghNextHint').className = 'gh-next-hint is-bad';
      $('ghNextHint').textContent = 'If you already linked billing in Google Cloud, click Refresh billing status. Otherwise open the billing console, then refresh.';
    } else {
      blockedText.textContent = 'Next locked — finish this step first';
      $('ghNextHint').className = 'gh-next-hint is-bad';
      $('ghNextHint').textContent = `Click “${doBtn.textContent}”, wait for checks to pass, then Next unlocks.`;
    }
  }

  let pendingGoogleHomePairing = null;
  let pairingInFlight = false;
  async function refreshGoogleHomePairing() {
    if (pairingInFlight) return;
    pairingInFlight = true;
    try {
      const result = await api('/api/google-home/pairing');
      pendingGoogleHomePairing = result?.pending || null;
      $('ghPairingCard').hidden = !pendingGoogleHomePairing;
      $('ghPairingCode').textContent = pendingGoogleHomePairing?.code || '';
      $('ghPairingStatus').textContent = pendingGoogleHomePairing ? 'A Google Home link is waiting for approval.' : 'No link request waiting.';
    } catch {
      pendingGoogleHomePairing = null;
      $('ghPairingCard').hidden = true;
      $('ghPairingStatus').textContent = 'Link approval is unavailable. Check the gateway connection.';
    } finally { pairingInFlight = false; }
  }

  $('ghPairingApprove').addEventListener('click', async () => {
    const pending = pendingGoogleHomePairing;
    if (!pending || !window.confirm(`Does ${pending.code} match the code shown during Google Home linking?`)) return;
    const button = $('ghPairingApprove');
    button.disabled = true;
    try {
      await api('/api/google-home/pairing/approve', {method:'POST', body:JSON.stringify(pending)});
      pendingGoogleHomePairing = null;
      $('ghPairingCard').hidden = true;
      announce('Approved. Return to Google Home to finish linking.');
    } catch (err) {
      announce(err.message || 'Approval failed. Start linking again.');
      await refreshGoogleHomePairing();
    } finally { button.disabled = false; }
  });

  async function refreshGoogleHome(opts = {}) {
    refreshGoogleHomePairing();
    try {
      const data = await api('/api/google-home/status');
      const sync = data.catalog_sync || {};
      const setup = data.setup || {};
      ghSetupCache = setup;
      const ready = Boolean(setup.cloud_run_url && setup.link_confirmed);
      $('ghLinkLabel').textContent = ready ? 'Linked' : (setup.cloud_run_url ? 'Configured — link pending' : 'Not configured');
      $('ghLinkState').classList.toggle('is-linked', ready);
      const syncLabel = $('ghSyncLabel');
      if (syncLabel) {
        syncLabel.hidden = false;
        syncLabel.textContent = sync.label || sync.status || '';
      }
      $('speakerSyncStatus').textContent = `Google Home sync: ${sync.label || sync.status || 'unknown'}${sync.note ? ' · ' + sync.note : ''}`;
      $('ghSyncNote').hidden = false;
      $('ghSyncNote').textContent = sync.note || '';
      $('ghSetupBtn').textContent = ghWizardOpen ? 'Setup in progress' : (setup.completed_steps?.length ? 'Resume Google Home setup' : 'Set up Google Home');
      if (ghWizardOpen && opts.followServerStep && setup.current_step) {
        const idx = GH_STEPS.findIndex((s) => s.id === setup.current_step);
        if (idx >= 0) ghStepIndex = idx;
      }
      if (ghWizardOpen) {
        renderGhWizardStep();
        await refreshCompanionStatus();
      }
    } catch (err) {
      $('ghLinkLabel').textContent = 'Unavailable';
      $('speakerSyncStatus').textContent = 'Google Home sync status unavailable.';
    }
  }

  function renderGhWizardStep() {
    const step = GH_STEPS[ghStepIndex] || GH_STEPS[0];
    $('ghWizardStepLabel').textContent = `Step ${ghStepIndex + 1} of ${GH_STEPS.length} — ${step.title}`;
    $('ghWizardBody').textContent = step.body;
    renderGhStepper();
    renderGhFields();
    ghStepVerified = Boolean((ghSetupCache.verified || {})[step.id]);
    if (ghSetupCache.last_verify?.step === step.id) {
      renderGhChecks(ghSetupCache.last_verify.checks || []);
      if (ghSetupCache.last_verify.ok === false) {
        $('ghNextHint').className = 'gh-next-hint is-bad';
        $('ghNextHint').textContent = 'Fix the failed checks, then try again.';
      }
    } else {
      renderGhChecks([]);
    }
    updateGhActionButtons();
  }

  function collectStepPatch() {
    const step = GH_STEPS[ghStepIndex];
    const patch = {};
    if (step.id === 'cloud_auth') {
      const projectId = $('ghProjectId')?.value?.trim();
      if (projectId) patch.project_id = projectId;
      const mode = document.querySelector('input[name="ghProjectMode"]:checked')?.value || 'create_new_project';
      patch.mode = mode;
      patch.prefer_new_project = mode !== 'attach_existing';
      if (ghCompanionAuth) patch.companion_auth = ghCompanionAuth;
    }
    if (step.id === 'prepare_services') {
      const progress = ghSetupCache.provision_progress || {};
      patch.billing_confirmed = Boolean(ghSetupCache.billing_confirmed) || progress.confirm_billing === 'ok';
      if (ghSetupCache.project_id) {
        patch.billing_url = `https://console.cloud.google.com/billing/linkedaccount?project=${encodeURIComponent(ghSetupCache.project_id)}`;
      }
    }
    if (step.id === 'subscriber_auth') {
      const key = $('ghCatalogWriteKey')?.value?.trim();
      if (key) patch.catalog_write_key = key;
      if (ghSetupCache.cloud_run_url) patch.cloud_gateway_url = ghSetupCache.cloud_run_url;
    }
    if (step.id === 'deploy_gateway') {
      const url = $('ghCloudRunUrl')?.value?.trim();
      if (url) patch.cloud_gateway_url = url;
    }
    if (step.id === 'developer_console') {
      const clientId = $('ghClientId')?.value?.trim();
      if (clientId) patch.client_id = clientId;
      patch.developer_console_done = Boolean(clientId) || Boolean(ghSetupCache.developer_console_done);
    }
    if (step.id === 'link_home') {
      patch.link_confirmed = true;
    }
    return patch;
  }

  function selectedCloudMode() {
    return document.querySelector('input[name="ghProjectMode"]:checked')?.value || ghSetupCache.mode || 'create_new_project';
  }

  async function createOrAttachGhProject() {
    const projectId = $('ghProjectId')?.value?.trim();
    if (!projectId) { announce('Enter a project ID first'); return; }
    const mode = selectedCloudMode();
    const attaching = mode === 'attach_existing';
    setGhBusy(true, attaching ? 'Checking existing project…' : 'Creating project…');
    try {
      await refreshCompanionStatus();
      const result = attaching
        ? await companionFetch('/projects/attach', {
            method: 'POST',
            body: JSON.stringify({ project_id: projectId }),
          })
        : await companionFetch('/projects/create', {
            method: 'POST',
            body: JSON.stringify({ project_id: projectId, confirm_billable: true }),
          });
      $('ghConsoleValues').textContent = JSON.stringify(result, null, 2);
      const progress = {
        ...(ghSetupCache.provision_progress || {}),
        ...(attaching
          ? { attach_project: result.status, create_project: result.status }
          : { create_project: result.status }),
      };
      const verify = await api('/api/google-home/wizard/verify', {
        method: 'POST',
        body: JSON.stringify({
          step: 'cloud_auth',
          patch: {
            project_id: projectId,
            prefer_new_project: !attaching,
            mode: attaching ? 'attach_existing' : 'create_new_project',
            provision_progress: progress,
            companion_auth: ghCompanionAuth,
          },
        }),
      });
      ghSetupCache = verify.setup || ghSetupCache;
      ghStepVerified = Boolean(verify.ok);
      renderGhChecks(verify.checks || []);
      announce(result.status === 'failed' ? (result.detail || 'Project action failed') : (attaching ? `Using existing project ${projectId}` : `Project ${result.status}`));
      updateGhActionButtons();
    } catch (err) {
      announce(err.message || 'Project action failed');
      $('ghNextHint').className = 'gh-next-hint is-bad';
      $('ghNextHint').textContent = err.message || 'Failed — check Advanced / logs';
    } finally {
      setGhBusy(false);
    }
  }

  async function runGhProvision() {
    const projectId = ghSetupCache.project_id || $('ghProjectId')?.value?.trim();
    if (!projectId) { announce('Set project ID in the previous step'); return; }
    const steps = ['confirm_billing', 'enable_apis', 'ensure_firestore', 'ensure_pubsub', 'ensure_service_account', 'ensure_catalog_bucket'];
    const labels = {
      confirm_billing: 'Checking billing link…',
      enable_apis: 'Enabling APIs (can take a few minutes)…',
      ensure_firestore: 'Creating Firestore…',
      ensure_pubsub: 'Creating Pub/Sub…',
      ensure_service_account: 'Creating service account…',
      ensure_catalog_bucket: 'Creating catalog bucket…',
    };
    const progress = { ...(ghSetupCache.provision_progress || {}) };
    setGhBusy(true, 'Provisioning…');
    try {
      await refreshCompanionStatus();
      for (const step of steps) {
        setGhBusy(true, labels[step] || `Provisioning ${step}…`);
        if ($('ghProvisionNote')) $('ghProvisionNote').textContent = `Running: ${step}…`;
        let result;
        try {
          result = await companionFetch('/provision/step', {
            method: 'POST',
            body: JSON.stringify({ project_id: projectId, step, confirm_billable: true, dry_run: false }),
          });
        } catch (err) {
          // Companion may return HTTP 500 with a structured result body
          progress[step] = 'failed';
          if ($('ghProvisionNote')) $('ghProvisionNote').textContent = `Failed at ${step}: ${err.message || err}`;
          $('ghConsoleValues').textContent = String(err.message || err);
          break;
        }
        const status = result.result?.status || result.status;
        progress[step] = status;
        if (result.result?.url) {
          progress.billing_url = result.result.url;
          ghSetupCache.billing_url = result.result.url;
        }
        if ($('ghProvisionNote')) $('ghProvisionNote').textContent = `Progress: ${step} → ${status}`;
        $('ghConsoleValues').textContent = JSON.stringify({ progress, last: result }, null, 2);
        await api('/api/google-home/wizard/verify', {
          method: 'POST',
          body: JSON.stringify({
            step: 'prepare_services',
            patch: {
              billing_confirmed: progress.confirm_billing === 'ok',
              billing_url: progress.billing_url || `https://console.cloud.google.com/billing/linkedaccount?project=${encodeURIComponent(projectId)}`,
              provision_progress: progress,
            },
          }),
        }).catch(() => null);
        if (status === 'needs_user') {
          announce('Billing still not linked');
          $('ghNextHint').className = 'gh-next-hint is-bad';
          $('ghNextHint').textContent = 'Billing not linked yet. Click Do this step to link billing, or open the billing console.';
          break;
        }
        if (status === 'failed') break;
      }
      const verify = await api('/api/google-home/wizard/verify', {
        method: 'POST',
        body: JSON.stringify({
          step: 'prepare_services',
          patch: {
            billing_confirmed: progress.confirm_billing === 'ok',
            billing_url: progress.billing_url || `https://console.cloud.google.com/billing/linkedaccount?project=${encodeURIComponent(projectId)}`,
            provision_progress: progress,
          },
        }),
      });
      ghSetupCache = verify.setup || ghSetupCache;
      ghStepVerified = Boolean(verify.ok);
      renderGhChecks(verify.checks || []);
      announce(verify.ok ? 'Provision verified — click Next' : 'Provision finished — fix failed checks');
      updateGhActionButtons();
      renderGhFields();
    } catch (err) {
      announce(err.message || 'Provision failed');
      $('ghNextHint').className = 'gh-next-hint is-bad';
      $('ghNextHint').textContent = err.message || 'Provision failed — open Advanced / logs';
    } finally {
      setGhBusy(false);
    }
  }

  async function mintGhSecrets() {
    const projectId = ghSetupCache.project_id;
    if (!projectId) { announce('Project ID missing'); return; }
    setGhBusy(true, 'Minting secrets…');
    try {
      await refreshCompanionStatus();
      const result = await companionFetch('/provision/step', {
        method: 'POST',
        body: JSON.stringify({ project_id: projectId, step: 'mint_local_secrets', confirm_billable: true, dry_run: false }),
      });
      const generated = result.result?.generated || {};
      $('ghConsoleValues').textContent = JSON.stringify({
        ...result,
        result: {
          ...(result.result || {}),
          generated: {
            ...generated,
            GOOGLE_CLIENT_SECRET: generated.GOOGLE_CLIENT_SECRET ? '[set]' : null,
            OAUTH_SIGNING_SECRET: generated.OAUTH_SIGNING_SECRET ? '[set]' : null,
            WEBHOOK_API_KEY: generated.WEBHOOK_API_KEY ? '[set]' : null,
          },
        },
      }, null, 2);
      if (generated.CATALOG_WRITE_KEY && $('ghCatalogWriteKey')) {
        $('ghCatalogWriteKey').value = generated.CATALOG_WRITE_KEY;
      }
      if (generated.ADMIN_PASSPHRASE) {
        const card = $('ghSecretsPassphraseCard');
        const ul = $('ghSecretsPasteList');
        if (card && ul) {
          card.hidden = false;
          ul.replaceChildren();
          const li = document.createElement('li');
          li.innerHTML = `<span class="label">Admin passphrase</span><code></code><button type="button" class="ghost-button">Copy</button>`;
          li.querySelector('code').textContent = generated.ADMIN_PASSPHRASE;
          li.querySelector('button').addEventListener('click', async () => {
            try { await copyText(generated.ADMIN_PASSPHRASE); announce('Copied Admin passphrase'); }
            catch (_) { announce('Copy failed — select the value manually'); }
          });
          ul.appendChild(li);
        }
      }
      const verify = await api('/api/google-home/wizard/verify', {
        method: 'POST',
        body: JSON.stringify({
          step: 'subscriber_auth',
          patch: {
            client_id: generated.GOOGLE_CLIENT_ID,
            catalog_write_key: generated.CATALOG_WRITE_KEY,
            cloud_gateway_url: ghSetupCache.cloud_run_url || undefined,
          },
        }),
      });
      ghSetupCache = verify.setup || ghSetupCache;
      ghStepVerified = Boolean(verify.ok);
      renderGhChecks(verify.checks || []);
      announce(verify.ok ? 'Secrets saved — save the admin passphrase for linking' : 'Secrets minted — review checks');
      updateGhActionButtons();
    } catch (err) {
      announce(err.message || 'Mint failed');
    } finally {
      setGhBusy(false);
    }
  }

  async function deployGhGateway() {
    const projectId = ghSetupCache.project_id;
    if (!projectId) { announce('Project ID missing'); return; }
    setGhBusy(true, 'Deploying Cloud Run… this can take a few minutes');
    try {
      await refreshCompanionStatus();
      const result = await companionFetch('/provision/step', {
        method: 'POST',
        body: JSON.stringify({ project_id: projectId, step: 'deploy_cloud_run', confirm_billable: true, dry_run: false }),
      });
      $('ghConsoleValues').textContent = JSON.stringify(result, null, 2);
      const url = result.result?.cloud_run_url || '';
      if (!url || result.result?.status === 'failed') {
        throw new Error(result.result?.detail || result.error || 'Deploy failed');
      }
      if ($('ghCloudRunUrl')) $('ghCloudRunUrl').value = url;
      const verify = await api('/api/google-home/wizard/verify', {
        method: 'POST',
        body: JSON.stringify({
          step: 'deploy_gateway',
          patch: { cloud_gateway_url: url },
        }),
      });
      ghSetupCache = verify.setup || ghSetupCache;
      ghStepVerified = Boolean(verify.ok);
      renderGhChecks(verify.checks || []);
      renderGhWizardStep();
      announce(verify.ok ? 'Gateway deployed — click Next' : 'Deployed — health check failed');
      updateGhActionButtons();
    } catch (err) {
      announce(err.message || 'Deploy failed');
    } finally {
      setGhBusy(false);
    }
  }

  async function verifyGhStep() {
    const step = GH_STEPS[ghStepIndex];
    setGhBusy(true, 'Verifying…');
    try {
      if (step.id === 'cloud_auth' || step.id === 'prepare_services' || step.id === 'subscriber_auth') {
        await refreshCompanionStatus();
      }
      const patch = collectStepPatch();
      if (step.id === 'cloud_auth' && ghCompanionAuth) patch.companion_auth = ghCompanionAuth;
      const result = await api('/api/google-home/wizard/verify', {
        method: 'POST',
        body: JSON.stringify({ step: step.id, patch }),
      });
      renderGhChecks(result.checks || []);
      ghStepVerified = Boolean(result.ok);
      ghSetupCache = result.setup || ghSetupCache;
      $('ghConsoleValues').textContent = JSON.stringify(result, null, 2);
      announce(result.ok ? 'Verified — click Next' : 'Verification failed — fix checks');
      updateGhActionButtons();
      renderGhStepper();
    } catch (err) {
      ghStepVerified = false;
      announce(err.message || 'Verify failed');
      updateGhActionButtons();
    } finally {
      setGhBusy(false);
    }
  }

  async function advanceGhStep() {
    const step = GH_STEPS[ghStepIndex];
    if (!ghStepVerified) {
      announce('Finish this step before Next');
      return;
    }
    const finishing = ghStepIndex >= GH_STEPS.length - 1;
    setGhBusy(true, finishing ? 'Finishing…' : 'Continuing…');
    try {
      await api('/api/google-home/wizard/advance', {
        method: 'POST',
        body: JSON.stringify({ step: step.id, patch: collectStepPatch() }),
      });
      if (finishing) {
        $('ghWizard').hidden = true;
        ghWizardOpen = false;
        announce('Google Home setup complete');
        await refreshGoogleHome();
        return;
      }
      ghStepIndex = Math.min(ghStepIndex + 1, GH_STEPS.length - 1);
      ghStepVerified = false;
      await refreshGoogleHome({ followServerStep: true });
    } catch (err) {
      announce(err.message || 'Could not continue');
    } finally {
      setGhBusy(false);
    }
  }

  async function persistBillingProgress(progress, billingOk) {
    const verify = await api('/api/google-home/wizard/verify', {
      method: 'POST',
      body: JSON.stringify({
        step: 'prepare_services',
        patch: {
          billing_confirmed: Boolean(billingOk),
          billing_url: progress.billing_url || `https://console.cloud.google.com/billing/linkedaccount?project=${encodeURIComponent(ghSetupCache.project_id || '')}`,
          provision_progress: progress,
        },
      }),
    });
    ghSetupCache = verify.setup || ghSetupCache;
    ghStepVerified = Boolean(verify.ok);
    renderGhChecks(verify.checks || []);
    updateGhActionButtons();
    renderGhFields();
    return verify;
  }

  async function checkOrLinkBilling() {
    const projectId = ghSetupCache.project_id;
    if (!projectId) { announce('Project ID missing'); return; }
    setGhBusy(true, 'Checking billing…');
    try {
      await refreshCompanionStatus();
      // Always re-read GCP first — console links don't update wizard state by themselves.
      const checked = await companionFetch('/provision/step', {
        method: 'POST',
        body: JSON.stringify({ project_id: projectId, step: 'confirm_billing', confirm_billable: true, dry_run: false }),
      });
      $('ghConsoleValues').textContent = JSON.stringify(checked, null, 2);
      let status = checked.result?.status || checked.status;
      const progress = { ...(ghSetupCache.provision_progress || {}) };
      if (checked.result?.url) progress.billing_url = checked.result.url;
      progress.confirm_billing = status;

      if (status === 'ok') {
        await persistBillingProgress(progress, true);
        announce('Billing is linked — provisioning next');
        setGhBusy(false);
        return runGhProvision();
      }

      // Not linked yet — try attaching the open Default Billing Account.
      setGhBusy(true, 'Linking billing…');
      const linked = await companionFetch('/provision/step', {
        method: 'POST',
        body: JSON.stringify({ project_id: projectId, step: 'link_billing', confirm_billable: true, dry_run: false }),
      });
      $('ghConsoleValues').textContent = JSON.stringify(linked, null, 2);
      status = linked.result?.status || linked.status;
      progress.link_billing = status;
      progress.confirm_billing = status === 'ok' ? 'ok' : status;
      if (linked.result?.url) progress.billing_url = linked.result.url;
      await persistBillingProgress(progress, status === 'ok');
      if (status === 'ok') {
        announce('Billing linked — provisioning next');
        setGhBusy(false);
        return runGhProvision();
      }
      announce(linked.result?.detail || checked.result?.detail || 'Billing still not linked');
    } catch (err) {
      announce(err.message || 'Billing check failed');
    } finally {
      setGhBusy(false);
    }
  }

  async function runGhDoStep() {
    if (ghBusy) return;
    if (ghDoMode === 'create_or_attach') return createOrAttachGhProject();
    if (ghDoMode === 'check_billing' || ghDoMode === 'link_billing') return checkOrLinkBilling();
    if (ghDoMode === 'provision') return runGhProvision();
    if (ghDoMode === 'mint') return mintGhSecrets();
    if (ghDoMode === 'deploy') return deployGhGateway();
    if (ghDoMode === 'mark_console') {
      const patch = collectStepPatch();
      if (!patch.client_id) { announce('Paste the OAuth client ID first'); return; }
      patch.developer_console_done = true;
      setGhBusy(true, 'Verifying…');
      try {
        const result = await api('/api/google-home/wizard/verify', {
          method: 'POST',
          body: JSON.stringify({ step: 'developer_console', patch }),
        });
        ghSetupCache = result.setup || ghSetupCache;
        ghStepVerified = Boolean(result.ok);
        renderGhChecks(result.checks || []);
        updateGhActionButtons();
        announce(result.ok ? 'Console step verified — click Next' : 'Console checks failed');
      } catch (err) {
        announce(err.message || 'Verify failed');
      } finally {
        setGhBusy(false);
      }
      return;
    }
    if (ghDoMode === 'confirm_link') {
      setGhBusy(true, 'Confirming…');
      try {
        await api('/api/google-home/link/confirm', { method: 'POST', body: JSON.stringify({ confirmed: true }) });
        const result = await api('/api/google-home/wizard/verify', {
          method: 'POST',
          body: JSON.stringify({ step: 'link_home', patch: { link_confirmed: true } }),
        });
        ghSetupCache = result.setup || ghSetupCache;
        ghStepVerified = Boolean(result.ok);
        renderGhChecks(result.checks || []);
        updateGhActionButtons();
        announce(result.ok ? 'Link confirmed — click Next' : 'Link confirmation failed');
      } catch (err) {
        announce(err.message || 'Confirm failed');
      } finally {
        setGhBusy(false);
      }
      return;
    }
    if (ghDoMode === 'test') {
      setGhBusy(true, 'Running test…');
      try {
        const test = await api('/api/google-home/test', { method: 'POST', body: '{}' });
        $('ghConsoleValues').textContent = JSON.stringify(test, null, 2);
        setGhBusy(false);
        return verifyGhStep();
      } catch (err) {
        announce(err.message || 'Test failed');
        setGhBusy(false);
      }
      return;
    }
    return verifyGhStep();
  }

  $('ghSetupBtn').addEventListener('click', async () => {
    ghWizardOpen = true;
    $('ghWizard').hidden = false;
    ghStepIndex = 0;
    ghStepVerified = false;
    bindGhPairingInput();
    renderGhWizardStep();
    await refreshCompanionStatus();
    await refreshGoogleHome({ followServerStep: true });
  });
  $('ghWizardHide').addEventListener('click', () => {
    $('ghWizard').hidden = true;
    ghWizardOpen = false;
    $('ghSetupBtn').textContent = 'Resume Google Home setup';
  });
  $('ghWizardHow').addEventListener('click', async () => {
    const drawer = $('ghTutorial');
    drawer.hidden = !drawer.hidden;
    if (!drawer.hidden) {
      try {
        const man = await fetch('/assets/tutorials/manifest.json').then((r) => r.json());
        const step = GH_STEPS[ghStepIndex]?.id;
        const entry = (man.steps || []).find((s) => s.id === step) || man.steps?.[0];
        const ul = $('ghTutorialLinks');
        ul.replaceChildren();
        (entry?.links || []).forEach((link) => {
          const li = document.createElement('li');
          li.innerHTML = `<a href="${link.href}" target="_blank" rel="noopener">${link.label}</a>`;
          ul.appendChild(li);
        });
        if (entry?.status === 'needs_capture') {
          const li = document.createElement('li');
          li.textContent = 'Recording status: needs_capture';
          ul.appendChild(li);
        }
      } catch (_) {
        $('ghTutorialLinks').innerHTML = '<li>Tutorial manifest unavailable</li>';
      }
    }
  });
  $('ghBackBtn').addEventListener('click', () => {
    if (ghBusy || ghStepIndex <= 0) return;
    ghStepIndex -= 1;
    ghStepVerified = Boolean((ghSetupCache.verified || {})[GH_STEPS[ghStepIndex].id]);
    renderGhWizardStep();
  });
  $('ghDoStepBtn').addEventListener('click', () => runGhDoStep());
  $('ghNextBtn').addEventListener('click', () => {
    if (!ghStepVerified || ghBusy) {
      announce('Finish this step before Next');
      return;
    }
    advanceGhStep();
  });
  async function syncGoogleHome() {
    try {
      await loadSpeakers(true);
      const res = await api('/api/google-home/sync', { method: 'POST', body: '{}' });
      const label = res.label || res.last_push_status || res.status || 'Sync requested';
      announce(label);
      await refreshGoogleHome();
    } catch (err) {
      announce(err.message || 'Sync failed');
    }
  }
  $('ghSyncBtn').addEventListener('click', syncGoogleHome);
  $('speakersSyncBtn').addEventListener('click', syncGoogleHome);



  return { refreshGoogleHome, refreshGoogleHomePairing };
}
