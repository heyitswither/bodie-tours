const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

(async () => {
  const screenshotDir = '/home/freya/bodie-tours/screenshots';
  if (!fs.existsSync(screenshotDir)) {
    fs.mkdirSync(screenshotDir, { recursive: true });
  }

  console.log("Launching Chrome...");
  const browser = await puppeteer.launch({
    executablePath: '/home/freya/usr/local/bin/google-chrome',
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
    defaultViewport: { width: 1280, height: 800 }
  });

  const page = await browser.newPage();

  async function runScenario(name, actionFn) {
    console.log(`\n========================================`);
    console.log(`Starting Scenario: ${name}`);
    console.log(`========================================`);
    const consoleLogs = [];
    const networkRequests = [];

    const onConsole = msg => {
      const logLine = `[${msg.type()}] ${msg.text()}`;
      consoleLogs.push(logLine);
      console.log(`Console: ${logLine}`);
    };
    const onRequest = req => {
      const reqLine = `${req.method()} ${req.url()}`;
      networkRequests.push(reqLine);
      console.log(`Network Request: ${reqLine}`);
    };

    page.on('console', onConsole);
    page.on('request', onRequest);

    try {
      await actionFn();
    } catch (err) {
      console.error(`Error in scenario ${name}:`, err);
    }

    page.off('console', onConsole);
    page.off('request', onRequest);

    // Save logs
    fs.writeFileSync(path.join(screenshotDir, `${name}_console.log`), consoleLogs.join('\n'));
    fs.writeFileSync(path.join(screenshotDir, `${name}_network.json`), JSON.stringify(networkRequests, null, 2));
    console.log(`Finished Scenario: ${name}\n`);
  }

  // Scenario 1: Happy Path
  await runScenario('happy_path', async () => {
    await page.goto('http://127.0.0.1:8081/', { waitUntil: 'networkidle0' });
    await page.waitForSelector('#bodie-booking-widget');

    // Select Private Town Tour card and advance to Step 1
    await page.click('.bb-tour-card[data-tour="private-town"]');
    await page.click('#bb-to-step-1');
    await page.waitForSelector('.bb-day.available');

    // Click June 15
    await page.evaluate(() => {
      const days = Array.from(document.querySelectorAll('.bb-day.available'));
      const day15 = days.find(el => el.textContent.trim() === '15');
      if (day15) {
        day15.click();
      } else {
        throw new Error("Day 15 available button not found");
      }
    });

    await page.click('#bb-to-step-2');
    await page.waitForSelector('.bb-slot:not(.full)');

    // Click first slot (10:00)
    await page.evaluate(() => {
      const slots = Array.from(document.querySelectorAll('.bb-slot:not(.full)'));
      if (slots.length > 0) {
        slots[0].click();
      } else {
        throw new Error("Available time slot not found");
      }
    });

    await page.click('#bb-to-step-3');
    await page.waitForSelector('#guest-name');

    // Fill form
    await page.type('#guest-name', 'Jane Doe');
    await page.type('#guest-email', 'jane.doe@example.com');
    await page.type('#guest-phone', '555-555-1234');
    await page.type('#guest-party', '4');

    // Submit
    await page.click('#bb-confirm-booking-btn');
    await page.waitForSelector('#step-pane-4.active', { timeout: 10000 });

    // Screenshot
    await page.screenshot({ path: path.join(screenshotDir, 'happy_path.png') });
  });

  // Scenario 2: Sold Out Slot Handling
  await runScenario('sold_out', async () => {
    await page.goto('http://127.0.0.1:8081/', { waitUntil: 'networkidle0' });
    await page.waitForSelector('#bodie-booking-widget');

    // Select Private Town Tour card and advance to Step 1
    await page.click('.bb-tour-card[data-tour="private-town"]');
    await page.click('#bb-to-step-1');
    await page.waitForSelector('.bb-day.available');

    // Click June 15
    await page.evaluate(() => {
      const days = Array.from(document.querySelectorAll('.bb-day.available'));
      const day15 = days.find(el => el.textContent.trim() === '15');
      if (day15) day15.click();
    });

    await page.click('#bb-to-step-2');
    await page.waitForSelector('.bb-slot.full');

    // Try to click the sold out slot (13:00)
    await page.evaluate(() => {
      const fullSlot = Array.from(document.querySelectorAll('.bb-slot.full')).find(el => el.textContent.includes('13:00'));
      if (fullSlot) {
        fullSlot.click();
      }
    });

    // Screenshot
    await page.screenshot({ path: path.join(screenshotDir, 'sold_out.png') });
  });

  // Scenario 3: Empty Month with No Availability
  await runScenario('empty_month', async () => {
    await page.goto('http://127.0.0.1:8081/', { waitUntil: 'networkidle0' });
    await page.waitForSelector('#bodie-booking-widget');

    // Select Private Town Tour card and advance to Step 1
    await page.click('.bb-tour-card[data-tour="private-town"]');
    await page.click('#bb-to-step-1');
    await page.waitForSelector('.bb-day');

    // Click next month button (July 2026)
    await page.click('button[aria-label="Next month"]');
    await new Promise(resolve => setTimeout(resolve, 1000));

    // Screenshot
    await page.screenshot({ path: path.join(screenshotDir, 'empty_month.png') });
  });

  // Scenario 4: Form Validation Failures
  await runScenario('validation_failure', async () => {
    await page.goto('http://127.0.0.1:8081/', { waitUntil: 'networkidle0' });
    await page.waitForSelector('#bodie-booking-widget');

    // Select Private Town Tour card and advance to Step 1
    await page.click('.bb-tour-card[data-tour="private-town"]');
    await page.click('#bb-to-step-1');
    await page.waitForSelector('.bb-day.available');

    // Click June 15
    await page.evaluate(() => {
      const days = Array.from(document.querySelectorAll('.bb-day.available'));
      const day15 = days.find(el => el.textContent.trim() === '15');
      if (day15) day15.click();
    });

    await page.click('#bb-to-step-2');
    await page.waitForSelector('.bb-slot:not(.full)');

    // Click first slot (10:00)
    await page.evaluate(() => {
      const slots = Array.from(document.querySelectorAll('.bb-slot:not(.full)'));
      if (slots.length > 0) slots[0].click();
    });

    await page.click('#bb-to-step-3');
    await page.waitForSelector('#guest-name');

    // Fill invalid data: invalid email, party size 21
    await page.type('#guest-name', 'Jane Doe');
    await page.type('#guest-email', 'invalidemail');
    await page.type('#guest-phone', '555-555-1234');
    await page.type('#guest-party', '25');

    // Submit to trigger validation
    await page.click('#bb-confirm-booking-btn');
    await page.waitForSelector('#bb-error-box', { visible: true });

    // Screenshot
    await page.screenshot({ path: path.join(screenshotDir, 'validation_failure.png') });
  });

  // Scenario 5: Backend Error Simulation
  await runScenario('backend_error', async () => {
    await page.goto('http://127.0.0.1:8081/', { waitUntil: 'networkidle0' });
    await page.waitForSelector('#bodie-booking-widget');

    // Select Private Town Tour card and advance to Step 1
    await page.click('.bb-tour-card[data-tour="private-town"]');
    await page.click('#bb-to-step-1');
    await page.waitForSelector('.bb-day.available');

    // Click June 15
    await page.evaluate(() => {
      const days = Array.from(document.querySelectorAll('.bb-day.available'));
      const day15 = days.find(el => el.textContent.trim() === '15');
      if (day15) day15.click();
    });

    await page.click('#bb-to-step-2');
    await page.waitForSelector('.bb-slot:not(.full)');

    // Click first slot (10:00)
    await page.evaluate(() => {
      const slots = Array.from(document.querySelectorAll('.bb-slot:not(.full)'));
      if (slots.length > 0) slots[0].click();
    });

    await page.click('#bb-to-step-3');
    await page.waitForSelector('#guest-name');

    // Fill form with name that triggers a Conflict Error (409)
    await page.type('#guest-name', 'Conflict Error');
    await page.type('#guest-email', 'conflict@example.com');
    await page.type('#guest-phone', '555-555-1234');
    await page.type('#guest-party', '2');

    // Submit
    await page.click('#bb-confirm-booking-btn');
    await page.waitForSelector('#bb-error-box', { visible: true });

    // Screenshot
    await page.screenshot({ path: path.join(screenshotDir, 'backend_error.png') });
  });

  await browser.close();
  console.log('All scenarios finished!');
})();
