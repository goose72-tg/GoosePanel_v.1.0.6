const https = require('https');
const querystring = require('querystring');
const SteamUser = require('steam-user');
const SteamTotp = require('steam-totp');

const client = new SteamUser({
    enablePicsCache: false,
});

const [, , login, password, sharedSecret, targetsArg] = process.argv;
const MAX_RECONNECT_ATTEMPTS = 3;
const RECONNECT_DELAY_MS = 5000;
const REQUEST_DELAY_MS = 15000;

if (!login || !password || !sharedSecret || !targetsArg) {
    console.error('Usage: node add_game_library.js <login> <password> <shared_secret> <targets_csv>');
    console.error('Example: node add_game_library.js my_login my_pass my_secret "730,subid:1576481,https://store.steampowered.com/app/730/CounterStrike_2/,https://store.steampowered.com/sub/1576481/"');
    process.exit(1);
}

function extractTarget(value) {
    const raw = String(value || '').trim();
    if (!raw) {
        return null;
    }

    const lowerRaw = raw.toLowerCase();
    const subMatch = raw.match(/(?:\/sub\/|subid\D*)(\d+)/i);
    if (subMatch) {
        const id = Number.parseInt(subMatch[1], 10);
        return Number.isInteger(id) && id > 0 ? { type: 'sub', id } : null;
    }

    if (/^\d+$/.test(raw)) {
        const id = Number.parseInt(raw, 10);
        if (!Number.isInteger(id) || id <= 0) {
            return null;
        }

        if (lowerRaw.startsWith('sub')) {
            return { type: 'sub', id };
        }
        return { type: 'app', id };
    }

    const appMatch = raw.match(/\/app\/(\d+)/i);
    if (appMatch) {
        const id = Number.parseInt(appMatch[1], 10);
        return Number.isInteger(id) && id > 0 ? { type: 'app', id } : null;
    }

    return null;
}

const parsedTargets = targetsArg
    .split(',')
    .map((item) => extractTarget(item))
    .filter(Boolean);

const appIds = [];
const subIds = [];
const appSeen = new Set();
const subSeen = new Set();

parsedTargets.forEach((target) => {
    if (target.type === 'app') {
        if (!appSeen.has(target.id)) {
            appSeen.add(target.id);
            appIds.push(target.id);
        }
        return;
    }

    if (!subSeen.has(target.id)) {
        subSeen.add(target.id);
        subIds.push(target.id);
    }
});

if (appIds.length === 0 && subIds.length === 0) {
    console.error('No valid targets found. Pass AppIDs/SubIDs or Steam store links separated by comma.');
    process.exit(2);
}

let isShuttingDown = false;
let reconnectAttempts = 0;
let licenseFlowStarted = false;
let licenseFlowCompleted = false;
let webSessionId = null;
let webCookies = [];

function shutdown(code = 0) {
    if (isShuttingDown) {
        return;
    }
    isShuttingDown = true;

    try {
        client.logOff();
    } catch (_) {
        // noop
    }

    setTimeout(() => process.exit(code), 250);
}

function doLogOn() {
    client.logOn({
        accountName: login,
        password,
        twoFactorCode: SteamTotp.getAuthCode(sharedSecret),
        machineName: `library_adder_${login}`,
    });
}

function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function requestWebSession() {
    return new Promise((resolve, reject) => {
        const onWebSession = (sessionId, cookies) => {
            cleanup();
            resolve({ sessionId, cookies });
        };

        const onError = (err) => {
            cleanup();
            reject(err || new Error('Failed to establish web session'));
        };

        const cleanup = () => {
            client.removeListener('webSession', onWebSession);
            client.removeListener('error', onError);
        };

        client.once('webSession', onWebSession);
        client.once('error', onError);
        client.webLogOn();
    });
}

function postAddFreeLicense(subId) {
    return new Promise((resolve, reject) => {
        if (!webSessionId || !Array.isArray(webCookies) || webCookies.length === 0) {
            reject(new Error('No web session/cookies for addfreelicense'));
            return;
        }

        const payload = querystring.stringify({
            action: 'add_to_cart',
            sessionid: webSessionId,
            subid: subId,
        });

        const request = https.request(
            {
                method: 'POST',
                hostname: 'store.steampowered.com',
                path: '/checkout/addfreelicense',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'Content-Length': Buffer.byteLength(payload),
                    Cookie: webCookies.join('; '),
                    Origin: 'https://store.steampowered.com',
                    Referer: `https://store.steampowered.com/sub/${subId}/`,
                },
            },
            (response) => {
                let responseText = '';
                response.on('data', (chunk) => {
                    responseText += chunk;
                });
                response.on('end', () => {
                    if (response.statusCode !== 200) {
                        reject(new Error(`HTTP ${response.statusCode} for subid ${subId}: ${responseText.slice(0, 180)}`));
                        return;
                    }

                    let successFlag = null;
                    try {
                        const payloadJson = JSON.parse(responseText);
                        if (typeof payloadJson.success === 'boolean') {
                            successFlag = payloadJson.success;
                        } else if (typeof payloadJson.success === 'number') {
                            successFlag = payloadJson.success > 0;
                        }
                    } catch (_) {
                        // keep null
                    }

                    const lowerText = String(responseText || '').toLowerCase();
                    const alreadyOwned = lowerText.includes('already') || lowerText.includes('owned');
                    const ok = successFlag === true || alreadyOwned;
                    resolve({ ok, responseText, alreadyOwned });
                });
            }
        );

        request.on('error', (err) => reject(err));
        request.setTimeout(20000, () => request.destroy(new Error(`Timeout while activating subid ${subId}`)));
        request.write(payload);
        request.end();
    });
}

async function requestLicenses() {
    if (isShuttingDown || licenseFlowStarted || licenseFlowCompleted) {
        return;
    }

    licenseFlowStarted = true;

    try {
        if (appIds.length > 0) {
            for (const [index, appId] of appIds.entries()) {
                const grantedApps = await new Promise((resolve, reject) => {
                    client.requestFreeLicense([appId], (err, grantedPackageIDs, grantedAppIDs) => {
                        if (err) {
                            reject(err);
                            return;
                        }

                        const grantedAppsList = Array.isArray(grantedAppIDs) ? grantedAppIDs : [];
                        const grantedPackages = Array.isArray(grantedPackageIDs) ? grantedPackageIDs : [];
                        if (grantedAppsList.length === 0 && grantedPackages.length === 0) {
                            reject(new Error('requestFreeLicense returned no granted apps/packages'));
                            return;
                        }

                        resolve(grantedAppsList);
                    });
                });

                if (grantedApps.includes(appId)) {
                    console.log(`Успешно добавил игру в библиотеку ${appId}`);
                } else {
                    console.log(`Не удалось добавить в библиотеку ${appId}`);
                }

                if (index < appIds.length - 1) {
                    await delay(REQUEST_DELAY_MS);
                }
            }
        }

        if (subIds.length > 0) {
            if (!webSessionId || !Array.isArray(webCookies) || webCookies.length === 0) {
                const webSession = await requestWebSession();
                webSessionId = webSession.sessionId;
                webCookies = webSession.cookies;
            }

            for (const [index, subId] of subIds.entries()) {
                const result = await postAddFreeLicense(subId);
                if (result.ok) {
                    console.log(`Успешно добавил лицензию subid ${subId}`);
                } else {
                    console.log(`Не удалось добавить лицензию subid ${subId}`);
                }

                if (index < subIds.length - 1) {
                    await delay(REQUEST_DELAY_MS);
                }
            }
        }

        licenseFlowCompleted = true;
        shutdown(0);
    } catch (err) {
        licenseFlowStarted = false;

        const reason = String(err?.message || err).toLowerCase();
        const isNoConnection = reason.includes('noconnection') || reason.includes('timeout') || reason.includes('econnreset');
        if (!licenseFlowCompleted && isNoConnection && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
            reconnectAttempts += 1;
            webSessionId = null;
            webCookies = [];
            console.error(`License request failed: ${err?.message || err}. Retry ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS} in ${RECONNECT_DELAY_MS / 1000}s...`);
            setTimeout(() => {
                if (isShuttingDown || licenseFlowCompleted) {
                    return;
                }
                doLogOn();
            }, RECONNECT_DELAY_MS);
            return;
        }

        if (appIds.length > 0) {
            appIds.forEach((appId) => {
                console.log(`Не удалось добавить в библиотеку ${appId}`);
            });
        }
        if (subIds.length > 0) {
            subIds.forEach((subId) => {
                console.log(`Не удалось добавить лицензию subid ${subId}`);
            });
        }

        shutdown(3);
    }
}

client.on('loggedOn', () => {
    reconnectAttempts = 0;
    client.setPersona(SteamUser.EPersonaState.Online);
    requestLicenses();
});

client.on('webSession', (sessionID, cookies) => {
    webSessionId = sessionID;
    webCookies = cookies;
});

client.on('error', (err) => {
    const reason = String(err?.message || err || '').toLowerCase();
    const isNoConnection = reason.includes('noconnection') || reason.includes('timeout') || reason.includes('econnreset');
    if (!licenseFlowCompleted && isNoConnection && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        reconnectAttempts += 1;
        webSessionId = null;
        webCookies = [];
        licenseFlowStarted = false;
        console.error(`Steam error: ${err?.message || err}. Retry ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS} in ${RECONNECT_DELAY_MS / 1000}s...`);
        setTimeout(() => {
            if (isShuttingDown || licenseFlowCompleted) {
                return;
            }
            doLogOn();
        }, RECONNECT_DELAY_MS);
        return;
    }

    console.error(`Steam error: ${err?.message || err}`);
    shutdown(5);
});

client.on('disconnected', (eresult, msg) => {
    const reason = msg || eresult;
    const isNoConnection = String(reason).toLowerCase().includes('noconnection');
    if (!licenseFlowCompleted && isNoConnection && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        reconnectAttempts += 1;
        webSessionId = null;
        webCookies = [];
        licenseFlowStarted = false;
        console.error(`Disconnected: ${reason}. Retry ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS} in ${RECONNECT_DELAY_MS / 1000}s...`);
        setTimeout(() => {
            if (isShuttingDown || licenseFlowCompleted) {
                return;
            }
            doLogOn();
        }, RECONNECT_DELAY_MS);
        return;
    }

    console.error(`Disconnected: ${msg || eresult}`);
    shutdown(6);
});

process.on('SIGINT', () => shutdown(0));
process.on('SIGTERM', () => shutdown(0));

doLogOn(); 
