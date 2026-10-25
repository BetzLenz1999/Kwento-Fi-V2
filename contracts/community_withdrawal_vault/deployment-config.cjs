"use strict";

const { id } = require("ethers");

// Public, immutable project namespace approved by the owner.
const COMMUNITY_ID_TEXT = "betzlenz@goodmarketph.v2";
const COMMUNITY_PROJECT_ID = id(COMMUNITY_ID_TEXT);
const CELO_MAINNET_CHAIN_ID = 42220;

module.exports = {
  COMMUNITY_ID_TEXT,
  COMMUNITY_PROJECT_ID,
  CELO_MAINNET_CHAIN_ID
};
