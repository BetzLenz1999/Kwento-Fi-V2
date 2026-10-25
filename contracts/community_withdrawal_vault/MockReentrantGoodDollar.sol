// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @dev Adversarial local token mock that attempts a configured callback during
///      transfers. Never use this contract as the production G$ token.
contract MockReentrantGoodDollar is ERC20 {
    address public callbackTarget;
    bytes public callbackData;
    bool public callbackAttempted;
    bool public callbackUsed;
    bool public nestedCallSucceeded;

    constructor() ERC20("Reentrant Mock GoodDollar", "rG$") {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }

    function configureCallback(address target, bytes calldata data) external {
        callbackTarget = target;
        callbackData = data;
        callbackAttempted = false;
        callbackUsed = false;
        nestedCallSucceeded = false;
    }

    function _update(address from, address to, uint256 value) internal override {
        if (from != address(0) && to != address(0) && callbackTarget != address(0) && !callbackUsed) {
            callbackUsed = true;
            callbackAttempted = true;
            (nestedCallSucceeded, ) = callbackTarget.call(callbackData);
        }
        super._update(from, to, value);
    }
}
