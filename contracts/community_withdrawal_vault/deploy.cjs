"use strict";

const { ethers, network } = require("hardhat");
const {
  COMMUNITY_ID_TEXT,
  COMMUNITY_PROJECT_ID,
  CELO_MAINNET_CHAIN_ID
} = require("./deployment-config.cjs");

const DECIMALS_ABI = ["function decimals() view returns (uint8)"];

function requiredEnv(name) {
  const value = process.env[name];
  if (!value || !value.trim()) throw new Error(`Missing required environment setting: ${name}`);
  return value.trim();
}

function requiredAddress(name) {
  try {
    return ethers.getAddress(requiredEnv(name));
  } catch {
    throw new Error(`${name} must be a valid EVM address`);
  }
}

function requiredPositiveInteger(name) {
  const raw = requiredEnv(name);
  if (!/^\d+$/.test(raw)) throw new Error(`${name} must be an integer in token base units`);
  const value = BigInt(raw);
  if (value <= 0n) throw new Error(`${name} must be greater than zero`);
  return value;
}

function sameAddress(left, right) {
  return left.toLowerCase() === right.toLowerCase();
}

async function main() {
  if (network.name !== "celo") {
    throw new Error("Refusing deployment: use the explicitly configured --network celo target.");
  }

  const rpcUrl = requiredEnv("CELO_RPC_URL");
  let parsedRpcUrl;
  try {
    parsedRpcUrl = new URL(rpcUrl);
  } catch {
    throw new Error("CELO_RPC_URL must be a valid HTTPS RPC URL");
  }
  if (parsedRpcUrl.protocol !== "https:") {
    throw new Error("CELO_RPC_URL must use HTTPS for Celo mainnet deployment");
  }

  const privateKey = requiredEnv("DEPLOYER_PRIVATE_KEY");
  if (!/^0x[0-9a-fA-F]{64}$/.test(privateKey)) {
    throw new Error("DEPLOYER_PRIVATE_KEY must be a 32-byte 0x-prefixed key supplied through a protected environment");
  }

  const networkInfo = await ethers.provider.getNetwork();
  if (networkInfo.chainId !== BigInt(CELO_MAINNET_CHAIN_ID)) {
    throw new Error(`Wrong network: expected Celo chain ${CELO_MAINNET_CHAIN_ID}, got ${networkInfo.chainId}`);
  }

  const [deployer] = await ethers.getSigners();
  if (!deployer) throw new Error("No deployer signer is configured for the Celo network");
  const deployerAddress = await deployer.getAddress();

  const tokenAddress = requiredAddress("GOODDOLLAR_TOKEN_ADDRESS");
  const issuerSignerAddress = requiredAddress("COMMUNITY_ISSUER_SIGNER_ADDRESS");
  const governanceAddress = requiredAddress("COMMUNITY_GOVERNANCE_MULTISIG_ADDRESS");
  if (sameAddress(issuerSignerAddress, governanceAddress)) {
    throw new Error("Issuer signer and governance multisig must be different addresses");
  }
  if (sameAddress(deployerAddress, issuerSignerAddress)) {
    throw new Error("Use a deployment signer separate from the Community issuer signer");
  }

  const tokenCode = await ethers.provider.getCode(tokenAddress);
  if (tokenCode === "0x") throw new Error("GOODDOLLAR_TOKEN_ADDRESS has no contract code on the selected chain");
  const governanceCode = await ethers.provider.getCode(governanceAddress);
  if (governanceCode === "0x") throw new Error("Governance must be a deployed multisig contract on the selected chain");

  const decimalsRaw = requiredEnv("GOODDOLLAR_EXPECTED_DECIMALS");
  if (!/^\d{1,2}$/.test(decimalsRaw)) {
    throw new Error("GOODDOLLAR_EXPECTED_DECIMALS must be an integer from 0 to 36, verified independently");
  }
  const expectedDecimals = Number(decimalsRaw);
  if (expectedDecimals > 36) throw new Error("GOODDOLLAR_EXPECTED_DECIMALS must be from 0 to 36");
  const tokenMetadata = new ethers.Contract(tokenAddress, DECIMALS_ABI, ethers.provider);
  const actualDecimals = Number(await tokenMetadata.decimals());
  if (actualDecimals !== expectedDecimals) {
    throw new Error(`Token decimals mismatch: configured ${expectedDecimals}, on-chain ${actualDecimals}`);
  }

  const minimumAmount = requiredPositiveInteger("COMMUNITY_MIN_WITHDRAWAL_BASE_UNITS");
  const maximumAmount = requiredPositiveInteger("COMMUNITY_MAX_WITHDRAWAL_BASE_UNITS");
  const dailyCap = requiredPositiveInteger("COMMUNITY_DAILY_CAP_BASE_UNITS");
  if (maximumAmount < minimumAmount) throw new Error("Maximum withdrawal must be at least the minimum");
  if (dailyCap < maximumAmount) throw new Error("Daily outflow cap must be at least the per-withdrawal maximum");

  const deployerBalance = await ethers.provider.getBalance(deployerAddress);
  const Vault = await ethers.getContractFactory("CommunityWithdrawalVault", deployer);
  const constructorArgs = [
    tokenAddress,
    issuerSignerAddress,
    governanceAddress,
    COMMUNITY_PROJECT_ID,
    CELO_MAINNET_CHAIN_ID,
    minimumAmount,
    maximumAmount,
    dailyCap
  ];
  const deployRequest = await Vault.getDeployTransaction(...constructorArgs);
  const estimatedGas = await ethers.provider.estimateGas({ ...deployRequest, from: deployerAddress });
  const feeData = await ethers.provider.getFeeData();
  const estimatedGasPrice = feeData.maxFeePerGas ?? feeData.gasPrice;
  if (estimatedGasPrice === null) throw new Error("Could not estimate Celo gas price; stopping without deployment");
  const estimatedCost = estimatedGas * estimatedGasPrice;
  if (deployerBalance < (estimatedCost * 120n) / 100n) {
    throw new Error("Deployer balance is below the estimated deployment cost plus a 20% buffer");
  }

  console.log("Celo mainnet deployment configuration (no secrets):");
  console.log(JSON.stringify({
    network: "celo",
    chainId: networkInfo.chainId.toString(),
    communityIdText: COMMUNITY_ID_TEXT,
    communityProjectIdBytes32: COMMUNITY_PROJECT_ID,
    tokenAddress,
    tokenDecimals: actualDecimals,
    issuerSignerAddress,
    governanceAddress,
    deployerAddress,
    minimumWithdrawalBaseUnits: minimumAmount.toString(),
    maximumWithdrawalBaseUnits: maximumAmount.toString(),
    dailyOutflowCapBaseUnits: dailyCap.toString(),
    estimatedGas: estimatedGas.toString(),
    estimatedGasCostWei: estimatedCost.toString()
  }, null, 2));

  if (process.env.CONFIRM_CELO_MAINNET_DEPLOYMENT !== "YES") {
    throw new Error("No deployment submitted. After reviewing the printed configuration, set CONFIRM_CELO_MAINNET_DEPLOYMENT=YES to authorize this script to broadcast.");
  }

  const vault = await Vault.deploy(...constructorArgs);
  const deploymentTx = vault.deploymentTransaction();
  if (!deploymentTx) throw new Error("Could not obtain the deployment transaction; inspect the wallet/network before retrying");
  console.log(`Deployment transaction submitted: ${deploymentTx.hash}`);

  const receipt = await deploymentTx.wait(2);
  if (!receipt || receipt.status !== 1) throw new Error("Deployment receipt was not successful; do not blindly resubmit");
  const vaultAddress = await vault.getAddress();

  const deployedConfiguration = {
    token: await vault.goodDollarToken(),
    issuer: await vault.issuerSigner(),
    governance: await vault.governance(),
    projectId: await vault.communityProjectId(),
    chainId: await vault.expectedChainId(),
    minimum: await vault.minimumWithdrawalAmount(),
    maximum: await vault.maximumWithdrawalAmount(),
    dailyCap: await vault.dailyOutflowCap()
  };
  if (!sameAddress(deployedConfiguration.token, tokenAddress)
      || !sameAddress(deployedConfiguration.issuer, issuerSignerAddress)
      || !sameAddress(deployedConfiguration.governance, governanceAddress)
      || deployedConfiguration.projectId !== COMMUNITY_PROJECT_ID
      || deployedConfiguration.chainId !== BigInt(CELO_MAINNET_CHAIN_ID)
      || deployedConfiguration.minimum !== minimumAmount
      || deployedConfiguration.maximum !== maximumAmount
      || deployedConfiguration.dailyCap !== dailyCap) {
    throw new Error(`Deployed address ${vaultAddress} did not report the expected immutable configuration; stop and investigate`);
  }

  console.log("Vault deployed and immutable configuration verified:");
  console.log(JSON.stringify({
    vaultAddress,
    deploymentTransactionHash: receipt.hash,
    communityIdText: COMMUNITY_ID_TEXT,
    communityProjectIdBytes32: COMMUNITY_PROJECT_ID,
    chainId: CELO_MAINNET_CHAIN_ID,
    tokenAddress,
    issuerSignerAddress,
    governanceAddress
  }, null, 2));
}

main().catch((error) => {
  // Never print process.env or signer/key material.
  console.error(`Deployment stopped: ${error.message || "unknown error"}`);
  process.exitCode = 1;
});
