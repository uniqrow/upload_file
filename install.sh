#! /bin/bash
#+-----------------------------------------------------------------------------
#+ Name : install.sh
#+ Description : Initial Setup Script
#+ Usage : /path/to/script/install.sh
#+ Argument : none
#+ Return : 0  - Success
#+          99 - Failure
#+-----------------------------------------------------------------------------
#+-----------------------------------------------------------------------------
#+ 定数設定
#+-----------------------------------------------------------------------------
readonly LNG_RC_SUCCESS=0
readonly LNG_RC_FAILURE=99

#+-----------------------------------------------------------------------------
#+ 変数設定
#+-----------------------------------------------------------------------------
LNG_RC=0

#+-----------------------------------------------------------------------------
#+ 関数設定
#+-----------------------------------------------------------------------------
Flex_Exit(){
    echo "NG"
    printf '%-10s %-8s : インストール処理を中止しました。\n' $(date '+%Y/%m/%d %H:%M:%S')
    exit "${LNG_RC_FAILURE}"
}

Check_RC(){
    local _LNG_TGT_RC="${1}"
    if [[ "${_LNG_TGT_RC}" -ne "${LNG_RC_SUCCESS}"  ]]; then
        Flex_Exit
    else
        echo "OK"
    fi
}

#+-----------------------------------------------------------------------------
#+ 主処理
#+-----------------------------------------------------------------------------
#実行ユーザの確認
[[ $(whoami) = 'root' ]] || Check_RC "${LNG_RC_FAILURE}"

#開始メッセージ
printf '%-10s %-8s : インストール処理を開始します。\n' $(date '+%Y/%m/%d %H:%M:%S')

#1. yumのアップデート
printf '%-2s - %-30s : ' '1' 'yum update'
yum update -y >/dev/null 2>&1
Check_RC "${?}"

#2. Python3のインストール
printf '%-2s - %-30s : ' '2' 'Python3 install'
yum install -y python3 >/dev/null 2>&1
Check_RC "${?}"

#3. pipのアップグレード
printf '%-2s - %-30s : ' '3' 'pip3 upgrade'
/usr/bin/pip3 install --upgrade pip >/dev/null 2>&1
Check_RC "${?}"

#4. Ansibleのインストール
printf '%-2s - %-30s : ' '4' 'Ansible install'
/usr/local/bin/pip3 install ansible >/dev/null 2>&1
Check_RC "${?}"

#5. dockerのインストール
printf '%-2s - %-30s : ' '5' 'docker install'
yum install -y docker >/dev/null 2>&1
Check_RC "${?}"

#6. docker-composeのインストール
printf '%-2s - %-30s : ' '6' 'docker-compose install'
curl -L https://github.com/docker/compose/releases/download/1.16.1/docker-compose-`uname -s`-`uname -m` -o /usr/local/bin/docker-compose >/dev/null 2>&1
chmod +x /usr/local/bin/docker-compose
[ -f /usr/local/bin/docker-compose -a -x /usr/local/bin/docker-compose ] && Check_RC "${LNG_RC_SUCCESS}" || Check_RC "${LNG_RC_FAILURE}"

#7. dockerプロセスの起動
printf '%-2s - %-30s : ' '7' 'docker proc start'
systemctl enable docker >/dev/null 2>&1
systemctl start docker >/dev/null 2>&1
[ $(systemctl status docker | grep -cE "Load.+enabled|active") -eq 2 ] && Check_RC "${LNG_RC_SUCCESS}" || Check_RC "${LNG_RC_FAILURE}"

#8. gitbucketのイメージ取得
printf '%-2s - %-30s : ' '8' 'gitbucket image get'
/usr/bin/docker pull gitbucket/gitbucket >/dev/null 2>&1
Check_RC "${?}"

#9. mysqlのイメージ取得
printf '%-2s - %-30s : ' '9' 'MySQL image get'
/usr/bin/docker pull mysql >/dev/null 2>&1
Check_RC "${?}"

#10. GitBucket用ディレクトリの作成
printf '%-2s - %-30s : ' '10' 'GitBucket Directory make'
mkdir -p "/docker/gitbucket"
[[ -d "/docker/gitbucket" ]] && Check_RC "${LNG_RC_SUCCESS}" || Check_RC "${LNG_RC_FAILURE}"

#11. docker-compose.ymlの作成
printf '%-2s - %-30s : ' '10' 'GitBucket Directory make'
cat <<EOF > "/docker/gitbucket/docker-compose.yml"
version: '3.1'

services:
  gitbucket-db:
    image: mysql
    restart: always
    command: --default-authentication-plugin=mysql_native_password --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
    ports:
      - "3306:3306"
    volumes:
      - ./mysql_init:/docker-entrypoint-initdb.d
      - ./mysql_data:/var/lib/mysql
    environment:
      TZ: Asia/Tokyo
      MYSQL_ROOT_PASSWORD: qawsedrftgyhujikolp
      MYSQL_DATABASE: gitbucket
      MYSQL_USER: gitbucket
      MYSQL_PASSWORD: 1q2w3e4r5t6y7u8i9
    security_opt: #Measures mbind: Operation not permitted
      - seccomp:unconfined
  gitbucket:
    image: gitbucket/gitbucket
    restart: always
    ports:
      - 3000:3000
      - 8080:8080
      - 29418:29418
    environment:
      - TZ=Asia/Tokyo
      - GITBUCKET_DB_URL=jdbc:mysql://gitbucket-db/gitbucket?useUnicode=true&characterEncoding=utf8
      - GITBUCKET_DB_USER=gitbucket
      - GITBUCKET_DB_PASSWORD=1q2w3e4r5t6y7u8i9
      - GITBUCKET_BASE_URL=
      - GITBUCKET_HOME=./gitbucket
    volumes:
      - ./gitbucket-data:/gitbucket
    depends_on:
      - gitbucket-db
    command: bash -c 'while !</dev/tcp/gitbucket-db/3306; do sleep 1; done; sleep 10; java -jar /opt/gitbucket.war'
EOF
[ -f "/docker/gitbucket/docker-compose.yml" -a ! -z "/docker/gitbucket/docker-compose.yml" ] && Check_RC "${LNG_RC_SUCCESS}" || Check_RC "${LNG_RC_FAILURE}"

#終了メッセージ
printf '%-10s %-8s : インストール処理を終了します。\n' $(date '+%Y/%m/%d %H:%M:%S')
exit "${LNG_RC_SUCCESS}"
