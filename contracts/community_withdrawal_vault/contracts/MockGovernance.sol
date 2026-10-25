// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @dev Local test harness only; this is NOT a production multisig.
contract MockGovernance {
    address public immutable controller;

    error UnauthorizedController();
    error TargetCallFailed(bytes returnData);

    constructor(address controller_) {
        require(controller_ != address(0), "zero controller");
        controller = controller_;
    }

    function execute(address target, bytes calldata callData) external returns (bytes memory result) {
        if (msg.sender != controller) revert UnauthorizedController();
        (bool success, bytes memory returnData) = target.call(callData);
        if (!success) revert TargetCallFailed(returnData);
        return returnData;
    }
}
