"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

function page() {
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, {innerHTML:"", textContent:"", disabled:false});
        return nodes.get(id);
    };
    const context = vm.createContext({window:{}, document:{addEventListener(){},getElementById:node},
        URLSearchParams, AbortController, setTimeout, clearTimeout, console});
    vm.runInContext(fs.readFileSync(path.join(__dirname,"../app/static/js/daily_research.js"),"utf8"), context);
    return {node, context, run:code=>vm.runInContext(code,context)};
}

function payload() {
    return {available:true, is_today:true, generated_at:"2026-11-06T12:52:00+08:00", as_of:"2026-11-05",
        title:"11月6日早报",body:"旧原文\n<img src=x onerror=bad>", label:"纸面 · 未验证",
        delivery:{status:"not_sent",label:"未发送：超过发送时段",message:"内容已生成，但未发送。"},evidence:[]};
}

test("saved body is escaped and data date is distinct from generation date",()=>{
    const p=page();p.context.data=payload();p.run("renderMorningBrief(data)");
    const html=p.node("morningBriefPanel").innerHTML;
    assert.match(html,/资料截至 2026-11-05/);assert.match(html,/生成 2026-11-06T12:52/);
    assert.match(html,/未发送：超过发送时段/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img/);
    assert.match(html,/不等于手机收到或已读/);
});

test("old or future record cannot masquerade as today's brief",()=>{
    const p=page();p.context.data={...payload(),is_today:false};p.run("renderMorningBrief(data)");
    assert.match(p.node("morningBriefPanel").innerHTML,/历史生成记录，不能当作今天早报/);
    p.context.data.is_future=true;p.run("renderMorningBrief(data)");
    assert.match(p.node("morningBriefPanel").innerHTML,/生成时间异常/);
    p.context.data.date_status_at_generation="stale";p.run("renderMorningBrief(data)");
    assert.match(p.node("morningBriefPanel").innerHTML,/生成时资料已过期/);
});

test("missing evidence never implies sent or zero failed attempts",()=>{
    const p=page();p.context.data={...payload(),delivery:{}};p.run("renderMorningBrief(data)");
    assert.match(p.node("morningBriefPanel").innerHTML,/发送结果未核实/);
    assert.doesNotMatch(p.node("morningBriefPanel").innerHTML,/已成功发送|发送成功|失败0/);
});

test("successful read is GET only and a later failure retains labelled previous content",async()=>{
    const p=page();p.context.data=payload();p.context.calls=[];
    p.run("requestJson=async (url, options)=>{calls.push([url,options]);return data;}");
    await p.run("loadMorningBrief()");
    assert.equal(p.context.calls[0][0],"/api/morning-brief");assert.equal(p.context.calls[0][1],undefined);
    p.run("requestJson=async()=>{throw new Error('offline')}");await p.run("loadMorningBrief()");
    assert.match(p.node("morningBriefPanel").innerHTML,/上次读取，本次结果未核实/);
    assert.match(p.node("morningBriefPanel").innerHTML,/旧原文/);
    assert.equal(p.node("morningBriefReadButton").disabled,false);
});

test("out of order reads cannot overwrite a newer result",async()=>{
    const p=page();p.context.data=payload();
    p.run("var finishOld;var count=0;requestJson=()=>++count===1?new Promise(resolve=>finishOld=resolve):Promise.resolve(data)");
    const old=p.run("loadMorningBrief()");await p.run("loadMorningBrief()");
    p.run("finishOld({...data,title:'过期请求'})");await old;
    assert.doesNotMatch(p.node("morningBriefPanel").innerHTML,/过期请求/);
});

test("home loads saved brief and template offers no resend button",()=>{
    const p=page();assert.ok(p.run("visiblePageLoaders('').includes(loadMorningBrief)"));
    const html=fs.readFileSync(path.join(__dirname,"../app/templates/daily_research.html"),"utf8");
    const section=html.match(/<section id="morningBriefSection"[\s\S]*?<\/section>/)[0];
    assert.match(section,/data-home-page="today"/);assert.match(section,/重新读取早报/);
    assert.doesNotMatch(section,/>补发|>发送|>重算/);
});
