//v.5
const SteamUser = require('steam-user');
const SteamCommunity = require('steamcommunity');
const SteamTotp = require('steam-totp');
const TradeOfferManager = require('steam-tradeoffer-manager');
const fs = require('fs');
const path = require('path');
const https = require('https');
const REPORT_SCHEMA_VERSION = 3;


process.on('uncaughtException', (err) => {
    console.log(`HandleError UncaughtException ${err && err.stack ? err.stack : err}`);
    process.exit(20);
});

process.on('unhandledRejection', (reason) => {
    console.log(`HandleError UnhandledRejection ${reason && reason.stack ? reason.stack : reason}`);
    process.exit(20);
});

const args = process.argv;

if (args.length < 7) {
    console.log("HandleError Not enough arguments");
    process.exit(20);
}

const login = args[2];
const password = args[3];
const shared_secret = args[4];
const identity_secret = args[5];
const tradeOfferLink = args[6];

let inventoryString = "730/2";

if (args[7]) {
    inventoryString = args[7];
}

function normalizeInventoryPairs(rawInventoryString) {
    const rawPairs = (rawInventoryString || "")
        .split(/[\s,;]+/)
        .map(pair => pair.trim())
        .filter(Boolean);

    const normalized = [];
    for (const pair of rawPairs) {
        const parts = pair.split('/').map(v => v.trim());
        if (parts.length !== 2) {
            console.log(`Skip invalid inventory pair format: ${pair}`);
            continue;
        }

        let [appId, contextId] = parts;

        // Частая опечатка: TF2 appid 440, а не 400
        if (appId === '400' && contextId === '2') {
            console.log('Detected likely typo 400/2, auto-correcting to 440/2');
            appId = '440';
        }

        if (!/^\d+$/.test(appId) || !/^\d+$/.test(contextId)) {
            console.log(`Skip invalid numeric inventory pair: ${pair}`);
            continue;
        }

        normalized.push(`${appId}/${contextId}`);
    }

    // уникализируем, сохраняя порядок
    return [...new Set(normalized)];
}

function getItemDisplayName(item) {
    return item.market_hash_name || item.market_name || item.name || `Item ${item.id || item.assetid || 'unknown'}`;
}

function fetchMarketPrice(appid, marketHashName) {
    return new Promise((resolve) => {
        if (!marketHashName) {
            resolve('N/A');
            return;
        }

        const query = new URLSearchParams({
            appid: String(appid),
            currency: '1',
            market_hash_name: marketHashName,
        }).toString();

        const url = `https://steamcommunity.com/market/priceoverview/?${query}`;

        const req = https.get(url, (res) => {
            let data = '';
            res.on('data', (chunk) => {
                data += chunk;
            });
            res.on('end', () => {
                try {
                    const parsed = JSON.parse(data);
                    if (parsed && parsed.success) {
                        resolve(parsed.lowest_price || parsed.median_price || 'N/A');
                        return;
                    }
                } catch (err) {
                    // ignore JSON parse errors; fallback below
                }
                resolve('N/A');
            });
        });

        req.on('error', () => resolve('N/A'));
        req.setTimeout(6000, () => {
            req.destroy();
            resolve('N/A');
        });
    });
}

function parsePriceToNumber(priceText) {
    if (!priceText || priceText === 'N/A') {
        return null;
    }

    const normalized = String(priceText).replace(',', '.');
    const match = normalized.match(/\d+(\.\d+)?/);
    if (!match) {
        return null;
    }

    const value = Number.parseFloat(match[0]);
    return Number.isFinite(value) ? value : null;
}

function formatMoney(value) {
    return Number.isFinite(value) ? value.toFixed(2) : 'N/A';
}

function formatDate(dateObj) {
    const d = String(dateObj.getDate()).padStart(2, '0');
    const m = String(dateObj.getMonth() + 1).padStart(2, '0');
    const y = dateObj.getFullYear();
    return `${d}/${m}/${y}`;
}

function formatArchiveDate(dateObj) {
    const d = String(dateObj.getDate()).padStart(2, '0');
    const m = String(dateObj.getMonth() + 1).padStart(2, '0');
    return `${d}.${m}`;
}

function makeUniqueFilePath(basePath) {
    if (!fs.existsSync(basePath)) {
        return basePath;
    }

    const ext = path.extname(basePath);
    const name = basePath.slice(0, -ext.length);

    let index = 1;
    let candidate = `${name}_${index}${ext}`;
    while (fs.existsSync(candidate)) {
        index += 1;
        candidate = `${name}_${index}${ext}`;
    }

    return candidate;
}

function getWednesdayPeriod(now = new Date()) {
    const date = new Date(now);
    date.setHours(0, 0, 0, 0);

    const wednesday = 3;
    const diff = (date.getDay() - wednesday + 7) % 7;

    const start = new Date(date);
    start.setDate(start.getDate() - diff);

    const end = new Date(start);
    end.setDate(end.getDate() + 7);

    return { start, end };
}

function createEmptyReportState(periodStart, periodEnd) {
    return {
        schemaVersion: REPORT_SCHEMA_VERSION,
        periodStart: periodStart.toISOString(),
        periodEnd: periodEnd.toISOString(),
        accounts: {},
        cases: {},
        skins: {},
        anotherDrops: {},
        totalCs2Drops: 0,
        totalCs2DropValue: 0,
        totalCases: 0,
        totalCaseValue: 0,
        totalSkins: 0,
        totalSkinValue: 0,
        totalAllSkins: 0,
        totalAllSkinValue: 0,
        totalAnotherDrops: 0,
        totalAnotherDropValue: 0,
    };
}

function archivePreviousWeekReport(savedState, reportPath, reportDataPath) {
    if (!savedState || !savedState.periodStart || !savedState.periodEnd) {
        return;
    }

    const reportDir = path.join(process.cwd(), 'report');
    fs.mkdirSync(reportDir, { recursive: true });

    const periodFrom = formatArchiveDate(new Date(savedState.periodStart));
    const periodTo = formatArchiveDate(new Date(savedState.periodEnd));
    const baseName = `${periodFrom}_${periodTo}`;

    const archivePath = makeUniqueFilePath(path.join(reportDir, `${baseName}.txt`));

    try {
        if (fs.existsSync(reportPath)) {
            fs.copyFileSync(reportPath, archivePath);
        } else {
            fs.writeFileSync(archivePath, renderGooseReport(savedState), 'utf8');
        }

        if (fs.existsSync(reportDataPath)) {
            const archiveJsonPath = archivePath.replace(/\.txt$/, '.json');
            fs.copyFileSync(reportDataPath, archiveJsonPath);
        }
    } catch (err) {
        // ignore archive errors
    }
}

function loadReportState(reportDataPath, reportPath) {
    const { start, end } = getWednesdayPeriod(new Date());
    const now = new Date();

    let state = null;
    if (fs.existsSync(reportDataPath)) {
        try {
            state = JSON.parse(fs.readFileSync(reportDataPath, 'utf8'));
        } catch (err) {
            state = null;
        }
    }

    if (!state || !state.periodStart || !state.periodEnd) {
        return createEmptyReportState(start, end);
    }

    const savedStart = new Date(state.periodStart);
    const savedEnd = new Date(state.periodEnd);

    if (!(now >= savedStart && now < savedEnd)) {
        archivePreviousWeekReport(state, reportPath, reportDataPath);
        return createEmptyReportState(start, end);
    }

    state.accounts = state.accounts || {};
    state.cases = state.cases || {};
    state.skins = state.skins || {};
    state.anotherDrops = state.anotherDrops || {};
    state.totalCs2Drops = Number(state.totalCs2Drops || 0);
    state.totalCs2DropValue = Number(state.totalCs2DropValue || 0);
    state.totalCases = Number(state.totalCases || 0);
    state.totalCaseValue = Number(state.totalCaseValue || 0);
    state.totalSkins = Number(state.totalSkins || 0);
    state.totalSkinValue = Number(state.totalSkinValue || 0);
    state.totalAllSkins = Number(state.totalAllSkins || state.totalSkins || 0);
    state.totalAllSkinValue = Number(state.totalAllSkinValue || state.totalSkinValue || 0);
    state.totalAnotherDrops = Number(state.totalAnotherDrops || 0);
    state.totalAnotherDropValue = Number(state.totalAnotherDropValue || 0);
    state.schemaVersion = REPORT_SCHEMA_VERSION;

    return state;
}

function addItemStat(targetMap, itemName, priceValue, priceText) {
    if (!targetMap[itemName]) {
        targetMap[itemName] = {
            amount: 0,
            totalPrice: 0,
            lastPriceText: priceText || 'N/A',
            pricedCount: 0,
        };
    }

    targetMap[itemName].amount += 1;
    if (priceValue !== null) {
        targetMap[itemName].totalPrice += priceValue;
        targetMap[itemName].pricedCount += 1;
    }
    if (priceText && priceText !== 'N/A') {
        targetMap[itemName].lastPriceText = priceText;
    }
}

function renderGooseReport(state) {
    const reportLines = [];
    const periodStart = new Date(state.periodStart);
    const periodEnd = new Date(state.periodEnd);

    const accountsCount = Object.keys(state.accounts || {}).length;
    const totalCs2Drops = state.totalCs2Drops || 0;

    reportLines.push('```');
    reportLines.push('💎 Goose Panel 💎');
    reportLines.push('');
    reportLines.push(`🗓 PERIOD: ${formatDate(periodStart)} - ${formatDate(periodEnd)}`);
    reportLines.push('');
    reportLines.push(`Accounts: ${accountsCount}`);
    reportLines.push('');

    reportLines.push('Case                               | Amount | % of drops');
    reportLines.push('-----------------------------------+--------+-----------');

    const cases = Object.entries(state.cases || {})
        .sort((a, b) => b[1].amount - a[1].amount || a[0].localeCompare(b[0]));

    if (!cases.length) {
        reportLines.push('No case drops                     | 0      | 0.0');
    } else {
        const totalCases = state.totalCases || 1;
        for (const [name, data] of cases) {
            const percent = ((data.amount / totalCases) * 100).toFixed(1);
            const caseName = name.length > 34 ? `${name.slice(0, 31)}...` : name;
            reportLines.push(`${caseName.padEnd(34)} | ${String(data.amount).padEnd(6)} | ${percent}`);
        }
    }

    reportLines.push('-----------------------------------+--------+-----------');
    reportLines.push('');

    reportLines.push('(Top 10 skins)                     | Amount | Price $');
    reportLines.push('-----------------------------------+--------+--------');

    const skins = Object.entries(state.skins || {})
        .sort((a, b) => {
            const priceA = a[1].pricedCount ? (a[1].totalPrice / a[1].pricedCount) : 0;
            const priceB = b[1].pricedCount ? (b[1].totalPrice / b[1].pricedCount) : 0;
            return priceB - priceA;
        })
        .slice(0, 10);

    if (!skins.length) {
        reportLines.push('No skins above threshold          | 0      | N/A');
    } else {
        for (const [name, data] of skins) {
            const avgPrice = data.pricedCount ? (data.totalPrice / data.pricedCount) : null;
            const skinName = name.length > 34 ? `${name.slice(0, 31)}...` : name;
            reportLines.push(`${skinName.padEnd(34)} | ${String(data.amount).padEnd(6)} | ${formatMoney(avgPrice)}`);
        }
    }

    reportLines.push('-----------------------------------+--------+--------');
    reportLines.push('');

    const totalValue = state.totalCs2DropValue || 0;
    const totalCases = state.totalCases || 0;
    const totalAllSkins = state.totalAllSkins || 0;
    const avgCasePrice = totalCases > 0 ? (state.totalCaseValue / totalCases) : 0;
    const avgSkinPrice = totalAllSkins > 0 ? (state.totalAllSkinValue / totalAllSkins) : 0;
    const avgDropPrice = avgCasePrice + avgSkinPrice;

    reportLines.push(`➙ Price of all drop: ~ ${formatMoney(totalValue)}$.`);
    reportLines.push(`➙ Total cases: ${totalCases} pcs.`);
    reportLines.push(`➙ AVG price of cases/all drop: ${formatMoney(avgCasePrice)}$/${formatMoney(avgDropPrice)}$.`);

    const anotherDrops = Object.entries(state.anotherDrops || {})
        .sort((a, b) => b[1].amount - a[1].amount || a[0].localeCompare(b[0]));

    if (anotherDrops.length) {
        reportLines.push('');
        reportLines.push('another drop:');
        for (const [name, data] of anotherDrops) {
            const avgPrice = data.pricedCount ? (data.totalPrice / data.pricedCount) : null;
            reportLines.push(`- ${name} | ${data.amount} | ${formatMoney(avgPrice)}$`);
        }
        reportLines.push(`➙ Price of another drop: ~ ${formatMoney(state.totalAnotherDropValue || 0)}$.`);
    }

    reportLines.push('```');

    return reportLines.join('\n');
}

async function buildAndWriteReport(sentItems, currentLogin) {
    if (!sentItems.length) {
        return;
    }

    const reportPath = path.join(process.cwd(), 'report.txt');
    const reportDataPath = path.join(process.cwd(), 'report_data.json');
    const reportState = loadReportState(reportDataPath, reportPath);

    reportState.accounts[currentLogin] = true;

    for (const item of sentItems) {
        const itemName = getItemDisplayName(item);
        const marketName = item.market_hash_name || item.market_name || item.name;
        const itemPriceText = await fetchMarketPrice(item.appid, marketName);
        const itemPriceValue = parsePriceToNumber(itemPriceText);

        const isCs2Drop = Number(item.appid) === 730 && String(item.contextid) === '2';
        if (!isCs2Drop) {
            reportState.totalAnotherDrops += 1;
            if (itemPriceValue !== null) {
                reportState.totalAnotherDropValue += itemPriceValue;
            }
            addItemStat(reportState.anotherDrops, itemName, itemPriceValue, itemPriceText);
            continue;
        }

        reportState.totalCs2Drops += 1;
        if (itemPriceValue !== null) {
            reportState.totalCs2DropValue += itemPriceValue;
        }

        const isCase = /case/i.test(itemName) || /sealed/i.test(itemName);
        if (isCase) {
            reportState.totalCases += 1;
            if (itemPriceValue !== null) {
                reportState.totalCaseValue += itemPriceValue;
            }
            addItemStat(reportState.cases, itemName, itemPriceValue, itemPriceText);
            continue;
        }

        const isWeaponSkin = itemName.includes(' | ');
        if (itemPriceValue !== null && isWeaponSkin) {
            reportState.totalAllSkins += 1;
            reportState.totalAllSkinValue += itemPriceValue;
        }

        if (itemPriceValue !== null && itemPriceValue > 0.6 && isWeaponSkin) {
            reportState.totalSkins += 1;
            reportState.totalSkinValue += itemPriceValue;
            addItemStat(reportState.skins, itemName, itemPriceValue, itemPriceText);
        }
    }

    fs.writeFileSync(reportDataPath, JSON.stringify(reportState, null, 2), 'utf8');
    fs.writeFileSync(reportPath, renderGooseReport(reportState), 'utf8');
}

function sendTrade() {
    const client = new SteamUser();
    const manager = new TradeOfferManager({
        "steam": client,
        "language": "en",
        "pollInterval": 5000
    });
    const community = new SteamCommunity();

    const logOnOptions = {
        "accountName": login,
        "password": password,
        "twoFactorCode": SteamTotp.getAuthCode(shared_secret)
    };

    client.logOn(logOnOptions);

    client.on('loggedOn', function() {
        console.log("Logged into Steam");
    });

    client.on('error', function(err) {
        errorHandler("Steam login error", err);
    });

    client.on('webSession', function(sessionID, cookies) {
        manager.setCookies(cookies, function(err) {
            errorHandler("Something went wrong while setting webSession", err);

            const inventories = normalizeInventoryPairs(inventoryString);
            console.log(`Using inventories: ${inventories.join(',') || 'none'}`);

            const csgoInventory = inventories.filter(inv => inv === '730/2');
            const otherInventories = inventories.filter(inv => inv !== '730/2');
            const sentItemsForReport = [];

            let completedTrades = 0;
            const totalTrades = (csgoInventory.length > 0 ? 1 : 0) + (otherInventories.length > 0 ? 1 : 0);

            if (totalTrades === 0) {
                console.log("No valid inventories provided");
                process.exit(11);
            }

            let sentOfferCount = 0;
            let noItemsTradesCount = 0;

            function sendTradeForInventories(inventoriesToSend, tradeName) {
                let offer = manager.createOffer(tradeOfferLink);
                let totalItemsCount = 0;
                let processedInventories = 0;
                let validInventories = 0;

                inventoriesToSend.forEach(inventoryPair => {
                    const [appId, contextId] = inventoryPair.split('/');

                    manager.getInventoryContents(
                        appId,
                        contextId,
                        true,
                        function(err, inventoryItems) {
                            processedInventories++;

                            if (err) {
                                console.log(`Inventory ${appId}:${contextId} is invalid or inaccessible: ${err.message}`);
                            } else {
                                const itemsCount = inventoryItems.length;
                                totalItemsCount += itemsCount;

                                inventoryItems.forEach(item => {
                                    const tradeItem = {
                                        appid: item.appid,
                                        contextid: item.contextid,
                                        assetid: (item.id || item.assetid).toString()
                                    };
                                    offer.addMyItem(tradeItem);
                                    sentItemsForReport.push(item);
                                });

                                validInventories++;
                            }

                            if (processedInventories === inventoriesToSend.length) {
                                if (validInventories === 0 || totalItemsCount === 0) {
                                    console.log(`No items to send for ${tradeName} trade (empty inventories)`);
                                    noItemsTradesCount++;
                                    completedTrades++;
                                    if (completedTrades === totalTrades) {
                                        finalizeExit();
                                    }
                                    return;
                                }

                                console.log(`${tradeName}: Total ${totalItemsCount} items to send from ${validInventories} valid inventories`);

                                offer.send(function(err, status) {
                                    errorHandler(`${tradeName} Something went wrong while sending trade offer`, err);

                                    if (status === 'pending') {
                                        console.log(`${tradeName} Offer #${offer.id} sent, but requires confirmation`);
                                        community.acceptConfirmationForObject(identity_secret, offer.id, function(err) {
                                            if (!err) {
                                                console.log(`${tradeName} Offer confirmed`);
                                                sentOfferCount++;
                                                completedTrades++;
                                                if (completedTrades === totalTrades) {
                                                    finalizeExit();
                                                }
                                            } else if (err && err.toString().includes("Could not act on confirmation")) {
                                                manager._community.httpRequestPost('https://steamcommunity.com//trade/new/acknowledge', {
                                                    "headers": {
                                                        "referer": "https://steamcommunity.com"
                                                    },
                                                    "json": true,
                                                    "form": {
                                                        "sessionid": manager._community.getSessionID(),
                                                        "message": 1
                                                    },
                                                    "checkJsonError": false,
                                                    "checkHttpError": false
                                                }, (err, response, body) => {
                                                    if (response.statusCode === 200) {
                                                        community.acceptConfirmationForObject(identity_secret, offer.id, function(err) {
                                                            if (!err) {
                                                                console.log(`${tradeName} Offer confirmed after HTTP request`);
                                                                sentOfferCount++;
                                                                completedTrades++;
                                                                if (completedTrades === totalTrades) {
                                                                    finalizeExit();
                                                                }
                                                            } else {
                                                                errorHandler(`${tradeName} Something went wrong during trade confirmation`, err);
                                                            }
                                                        });
                                                    } else {
                                                        errorHandler(`${tradeName} Something went wrong during trade confirmation`, err);
                                                    }
                                                });
                                            } else {
                                                errorHandler(`${tradeName} Something went wrong during trade confirmation`, err);
                                            }
                                        });
                                    } else {
                                        console.log(`${tradeName} Offer #${offer.id} sent successfully`);
                                        sentOfferCount++;
                                        completedTrades++;
                                        if (completedTrades === totalTrades) {
                                            finalizeExit();
                                        }
                                    }
                                });
                            }
                        }
                    );
                });
            }

            if (csgoInventory.length > 0) {
                sendTradeForInventories(csgoInventory, "CS:GO");
            }
            if (otherInventories.length > 0) {
                sendTradeForInventories(otherInventories, "Other");
            }

            async function finalizeExit() {
                if (sentOfferCount > 0) {
                    console.log(`SENT_ITEMS_COUNT:${sentItemsForReport.length}`);
                    try {
                        await buildAndWriteReport(sentItemsForReport, login);
                    } catch (err) {
                        // report ошибки не должны ломать успешную отправку трейда
                    }
                    process.exit(0);
                }

                if (noItemsTradesCount > 0) {
                    process.exit(10);
                }

                process.exit(20);
            }
        });

        community.setCookies(cookies);
    });
}

function errorHandler(message, error) {
    if (error) {
        console.log(`HandleError ${message} ${error}`);
        process.exit(20);
    }
}

sendTrade();
