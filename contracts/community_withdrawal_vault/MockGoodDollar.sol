// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @dev Local test token only. Never use this contract as the production G$ token.
contract MockGoodDollar is ERC20 {
    bool public transfersBlocked;

    error MockTransfersBlocked();

    constructor() ERC20("Mock GoodDollar", "mG$") {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }

    function setTransfersBlocked(bool blocked) external {
        transfersBlocked = blocked;
    }

    function _update(address from, address to, uint256 value) internal override {
        if (transfersBlocked && from != address(0) && to != address(0)) {
            revert MockTransfersBlocked();
        }
        super._update(from, to, value);
    }
}
