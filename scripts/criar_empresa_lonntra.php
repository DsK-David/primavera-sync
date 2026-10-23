<?php
/**
 * Cria na Lonntra a empresa que vai receber os dados do Primavera, pelos serviços da própria Lonntra
 * (Empresas::afterSave faz a configuração inicial: moeda CVE e série do ano, ADR-034), e dá acesso de
 * Administrador a um utilizador. Idempotente: se a empresa já existir (mesmo NIF ou nome), só mostra os IDs.
 *
 * A empresa fica SEM credenciais do e-Fatura (IAM_CLIENT_ID, IAM_CLIENT_SECRET, LED_CODE): os documentos já
 * foram emitidos e comunicados pelo Primavera, a Lonntra não os pode voltar a comunicar (Envio::faturasPorEnviar).
 *
 * Uso: php scripts/criar_empresa_lonntra.php <pasta da Lonntra> "<nome>" <NIF> [utilizador] [repositório]
 * repositório do e-Fatura: 1 Principal (empresa real, em produção), 2 Homologação, 3 Teste (por defeito)
 * Ex.: php scripts/criar_empresa_lonntra.php ../ilhapay "Auto Style" 299999970 david.silva
 */

if ($argc < 4) {
    fwrite(STDERR, "uso: php scripts/criar_empresa_lonntra.php <pasta da Lonntra> \"<nome>\" <NIF> [utilizador] [repositório 1|2|3]\n");
    exit(1);
}
[, $pasta, $nome, $nif] = $argv;
$utilizador = $argv[4] ?? 'david.silva';
$repositorio = $argv[5] ?? '3';
if (!in_array($repositorio, ['1', '2', '3'], true)) {
    fwrite(STDERR, "O repositório tem de ser 1 (Principal), 2 (Homologação) ou 3 (Teste).\n");
    exit(1);
}
$pasta = realpath($pasta);

defined('YII_DEBUG') or define('YII_DEBUG', false);
defined('YII_ENV') or define('YII_ENV', 'dev');
require $pasta . '/vendor/autoload.php';
require $pasta . '/vendor/yiisoft/yii2/Yii.php';
new yii\console\Application(require $pasta . '/config/console.php');

use app\components\core\SincronizacaoAcesso;
use app\components\faturacao\Ids;
use app\models\CoreVinculo;
use app\models\Empresas;
use yii\db\Query;

$user = (new Query())->from('glb_user')->where(['USERNAME' => $utilizador, 'ESTADO' => 'A'])->one();
if (!$user) {
    fwrite(STDERR, "Não existe o utilizador ativo «{$utilizador}».\n");
    exit(1);
}

$empresa = (new Query())->from('entidade')->where(['or', ['NIF' => $nif], ['DESIG' => $nome]])->one();
$db = Yii::$app->db;
$t = $db->beginTransaction();
try {
    if ($empresa) {
        echo "A empresa já existe: {$empresa['DESIG']} (NIF {$empresa['NIF']}).\n";
        $id = $empresa['ID'];
    } else {
        $agora = date('Y-m-d H:i:s');
        $m = new Empresas();
        $m->setAttributes([
            'ID' => Yii::$app->helperApp->uuid(),
            'CODIGO' => Yii::$app->helperApp->getCODIGO(new Empresas(), 5),
            'DESIG' => $nome,
            'DESCR' => $nome,
            'NIF' => $nif,
            'DT_REGISTO' => $agora,
            'DT_ALTERACAO' => $agora,
            'ESTADO' => 'A',
            'pr_enquadramento_ID' => Ids::ENQUAD_CONT_ORG,
            'pr_pais_ID' => 'CV',
            'pr_moeda_ID' => null,       // a configuração inicial cria o escudo (CVE)
            'FLAG_STOCK' => '0',         // o stock é gerido no Primavera
            'FLAG_IMPOTAR_COMPRAS_EFATURA' => '0',
            'IVA_DEFAULT' => 15,
            'IAM_CLIENT_ID' => null,
            'IAM_CLIENT_SECRET' => null,
            'LED_CODE' => null,
            'REPOSITORIO' => $repositorio,
            'FLAG_DEMO' => 'N',
        ], false);
        $m->isNewRecord = true;
        $m->save(false);
        $id = $m->ID;
        if (!(new Query())->from('pr_serie')->where(['Entidade_ID' => $id, 'ESTADO' => 'A'])->exists()) {
            throw new RuntimeException('A configuração inicial (série) não ficou feita.');
        }
        echo "Empresa criada: {$nome} (NIF {$nif}).\n";
    }

    $temVinculo = (new Query())->from('core_vinculo')
        ->where(['USER_ID' => $user['ID'], 'Entidade_ID' => $id, 'ESTADO' => CoreVinculo::ESTADO_ATIVO])->exists();
    if (!$temVinculo) {
        $papel = (new SincronizacaoAcesso())->papelModelo(Ids::PERFIL_ADMINISTRADOR, false);
        if ($papel === null) {
            throw new RuntimeException('Não há papel-modelo para o perfil Administrador (core_papel).');
        }
        $agora = date('Y-m-d H:i:s');
        $v = new CoreVinculo([
            'ID' => Yii::$app->helperApp->uuid(), 'USER_ID' => $user['ID'], 'Entidade_ID' => $id,
            'TIPO' => CoreVinculo::TIPO_INTERNO, 'ESTADO' => CoreVinculo::ESTADO_ATIVO, 'PADRAO' => '0',
            'DT_INICIO' => date('Y-m-d'), 'DT_REGISTO' => $agora,
        ]);
        if (!$v->save()) {
            throw new RuntimeException('Vínculo: ' . json_encode($v->errors));
        }
        $db->createCommand()->insert('core_vinculo_papel', [
            'ID' => Yii::$app->helperApp->uuid(), 'VINCULO_ID' => $v->ID, 'PAPEL_ID' => $papel->ID, 'DT_REGISTO' => $agora,
        ])->execute();
        echo "Acesso de Administrador dado a {$utilizador}.\n";
    }
    $t->commit();
} catch (Throwable $e) {
    $t->rollBack();
    fwrite(STDERR, 'Falhou: ' . $e->getMessage() . "\nNada ficou gravado.\n");
    exit(1);
}

echo "\n--- para o config.ini do primavera-sync ---\n";
echo "entidade_id = {$id}\n";
echo "utilizador_id = {$user['ID']}\n";
