import os

import rlp

from typing import (
    cast,
    Any,
    Dict,
    Iterable,
    Tuple,
    Type,
)

from eth_utils.toolz import first

from eth_utils import (
    to_normalized_address,
)

from eth import MainnetChain
from eth.abc import (
    BlockAPI,
    ChainAPI,
    StateAPI,
    VirtualMachineAPI,
)
from eth.db.atomic import AtomicDB
from eth.chains.mainnet import (
    MainnetDAOValidatorVM,
)
from eth.tools.builder.chain import (
    disable_pow_check,
)
from eth.typing import (
    AccountState,
)
from eth._utils.state import (
    diff_state,
)
from eth.vm.forks import (
    PetersburgVM,
    ConstantinopleVM,
    ByzantiumVM,
    TangerineWhistleVM,
    FrontierVM,
    HomesteadVM as BaseHomesteadVM,
    SpuriousDragonVM,
    IstanbulVM,
)


#
# State Setup
#
def setup_state(desired_state: AccountState, state: StateAPI) -> None:
    for account, account_data in desired_state.items():
        for slot, value in account_data['storage'].items():
            state.set_storage(account, slot, value)

        nonce = account_data['nonce']
        code = account_data['code']
        balance = account_data['balance']

        state.set_nonce(account, nonce)
        state.set_code(account, code)
        state.set_balance(account, balance)
    state.persist()


def verify_state(expected_state: AccountState, state: StateAPI) -> None:
    diff = diff_state(expected_state, state)
    new_line = "\n"
    if diff:
        error_messages = []
        for account, field, actual_value, expected_value in diff:
            if field == 'balance':
                error_messages.append(
                    f"{to_normalized_address(account)}(balance) | "
                    f"Actual: {actual_value!r} | Expected: {expected_value!r} | "
                    f"Delta: {cast(int, actual_value) - cast(int, expected_value)}"
                )
            else:
                error_messages.append(
                    f"{to_normalized_address(account)}({field}) | "
                    f"Actual: {actual_value!r} | Expected: {expected_value!r}"
                )
        raise AssertionError(
            f"State DB did not match expected state on {len(error_messages)} values:{new_line}"
            f"{f'{new_line} - '.join(error_messages)}"
        )


def chain_vm_configuration(fixture: Dict[str, Any]) -> Iterable[Tuple[int, Type[VirtualMachineAPI]]]:  # noqa: E501
    network = fixture['network']

    if network == 'Frontier':
        return (
            (0, FrontierVM),
        )
    elif network == 'Homestead':
        HomesteadVM = BaseHomesteadVM.configure(support_dao_fork=False)
        return (
            (0, HomesteadVM),
        )
    elif network == 'EIP150':
        return (
            (0, TangerineWhistleVM),
        )
    elif network == 'EIP158':
        return (
            (0, SpuriousDragonVM),
        )
    elif network == 'Byzantium':
        return (
            (0, ByzantiumVM),
        )
    elif network == 'Constantinople':
        return (
            (0, ConstantinopleVM),
        )
    elif network == 'ConstantinopleFix':
        return (
            (0, PetersburgVM),
        )
    elif network == 'Istanbul':
        return (
            (0, IstanbulVM),
        )
    elif network == 'FrontierToHomesteadAt5':
        HomesteadVM = BaseHomesteadVM.configure(support_dao_fork=False)
        return (
            (0, FrontierVM),
            (5, HomesteadVM),
        )
    elif network == 'HomesteadToEIP150At5':
        HomesteadVM = BaseHomesteadVM.configure(support_dao_fork=False)
        return (
            (0, HomesteadVM),
            (5, TangerineWhistleVM),
        )
    elif network == 'HomesteadToDaoAt5':
        HomesteadVM = MainnetDAOValidatorVM.configure(
            support_dao_fork=True,
            _dao_fork_block_number=5,
        )
        return (
            (0, HomesteadVM),
        )
    elif network == 'EIP158ToByzantiumAt5':
        return (
            (0, SpuriousDragonVM),
            (5, ByzantiumVM),
        )
    elif network == 'ByzantiumToConstantinopleFixAt5':
        return (
            (0, ByzantiumVM),
            (5, PetersburgVM),
        )
    else:
        raise ValueError(f"Network {network} does not match any known VM rules")


def genesis_params_from_fixture(fixture: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'parent_hash': fixture['genesisBlockHeader']['parentHash'],
        'uncles_hash': fixture['genesisBlockHeader']['uncleHash'],
        'coinbase': fixture['genesisBlockHeader']['coinbase'],
        'state_root': fixture['genesisBlockHeader']['stateRoot'],
        'transaction_root': fixture['genesisBlockHeader']['transactionsTrie'],
        'receipt_root': fixture['genesisBlockHeader']['receiptTrie'],
        'bloom': fixture['genesisBlockHeader']['bloom'],
        'difficulty': fixture['genesisBlockHeader']['difficulty'],
        'block_number': fixture['genesisBlockHeader']['number'],
        'gas_limit': fixture['genesisBlockHeader']['gasLimit'],
        'gas_used': fixture['genesisBlockHeader']['gasUsed'],
        'timestamp': fixture['genesisBlockHeader']['timestamp'],
        'extra_data': fixture['genesisBlockHeader']['extraData'],
        'mix_hash': fixture['genesisBlockHeader']['mixHash'],
        'nonce': fixture['genesisBlockHeader']['nonce'],
    }


def new_chain_from_fixture(fixture: Dict[str, Any],
                           chain_cls: Type[ChainAPI] = MainnetChain) -> ChainAPI:
    base_db = AtomicDB()

    vm_config = chain_vm_configuration(fixture)

    ChainFromFixture = chain_cls.configure(
        'ChainFromFixture',
        vm_configuration=vm_config,
    )

    if 'sealEngine' in fixture and fixture['sealEngine'] == 'NoProof':
        ChainFromFixture = disable_pow_check(ChainFromFixture)

    return ChainFromFixture.from_genesis(
        base_db,
        genesis_params=genesis_params_from_fixture(fixture),
        genesis_state=fixture['pre'],
    )


def apply_fixture_block_to_chain(
        block_fixture: Dict[str, Any],
        chain: ChainAPI,
        perform_validation: bool=True) -> Tuple[BlockAPI, BlockAPI, BlockAPI]:
    """
    :return: (premined_block, mined_block, rlp_encoded_mined_block)
    """
    # The block to import may be in a different block-class-range than the
    # chain's current one, so we use the block number specified in the
    # fixture to look up the correct block class.
    if 'blockHeader' in block_fixture:
        block_number = block_fixture['blockHeader']['number']
        block_class = chain.get_vm_class_for_block_number(block_number).get_block_class()
    else:
        block_class = chain.get_vm().get_block_class()

    block = rlp.decode(block_fixture['rlp'], sedes=block_class)

    import_result = chain.import_block(block, perform_validation=perform_validation)
    mined_block = import_result.imported_block

    rlp_encoded_mined_block = rlp.encode(mined_block, sedes=block_class)

    return (block, mined_block, rlp_encoded_mined_block)


def should_run_slow_tests() -> bool:
    if os.environ.get('TRAVIS_EVENT_TYPE') == 'cron':
        return True
    return False


def get_test_name(filler: Dict[str, Any]) -> str:
    assert len(filler) == 1
    return first(filler)
